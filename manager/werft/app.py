"""Composition root (SPEC §1 layering: the only module allowed to import
from every layer below it).

Lifespan wiring: the GitHub-backed orchestrator only starts when a GitHub
App client id is configured (`Settings.github_app_client_id`) — tests and
local dev boot clean, healthz-only, with no engine, no httpx client, and no
GitHub call ever made. When creds are present, the lifespan reads the
App's private key file, builds one engine, one `httpx.AsyncClient`, one
`AppAuth`, and two per-project `RepoOps` factories — a manager-permission
one (`MANAGER_PERMISSIONS`) for day-to-day polling and a transient
admin-permission one (`ADMIN_PERMISSIONS`) for the one branch-protection
call onboarding/the doctrine-#1 flip need (SPEC §6.3) — then starts
`Orchestrator.run` as a background task. Shutdown sets the stop event and
awaits that task before disposing the engine/http client, so a clean
SIGTERM/lifespan-exit drains every in-flight unit of work first.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from werft.api.routes import router
from werft.config.settings import Settings
from werft.db.engine import create_engine_from_url
from werft.db.models import Project
from werft.github.auth import ADMIN_PERMISSIONS, MANAGER_PERMISSIONS, AppAuth
from werft.github.client import GitHubClient
from werft.github.ops import RepoOps
from werft.observe.alerts import NullAlertSink
from werft.orchestrator.finalize import NullQuota
from werft.orchestrator.loop import Orchestrator


def _ops_factory(
    auth: AppAuth, http: httpx.AsyncClient, api_url: str, permissions: dict[str, str]
) -> Callable[[Project], RepoOps]:
    """A `Callable[[Project], RepoOps]` bound to one permission ceiling.
    The token provider is a zero-arg closure per project; `GitHubClient`
    calls it lazily on the first actual request, never at construction —
    building the callable here never itself mints a token or makes a
    GitHub call."""

    def build(project: Project) -> RepoOps:
        async def token_provider() -> str:
            token = await auth.token_for(project.github_owner, project.github_repo, permissions)
            return token.token

        client = GitHubClient(http, api_url=api_url, token_provider=token_provider)
        return RepoOps(client, project.github_owner, project.github_repo)

    return build


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not resolved.github_app_client_id:
            # No GitHub App configured: tests/dev boot clean, healthz-only —
            # no engine, no httpx client, no orchestrator, no GitHub call.
            yield
            return

        private_key_pem = Path(resolved.github_app_private_key_file).read_text()
        engine = create_engine_from_url(resolved.database_url)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        http = httpx.AsyncClient()
        auth = AppAuth(
            http,
            client_id=resolved.github_app_client_id,
            private_key_pem=private_key_pem,
            api_url=resolved.github_api_url,
        )
        orchestrator = Orchestrator(
            session_factory,
            _ops_factory(auth, http, resolved.github_api_url, MANAGER_PERMISSIONS),
            _ops_factory(auth, http, resolved.github_api_url, ADMIN_PERMISSIONS),
            alerts=NullAlertSink(),
            quota=NullQuota(),
            settings=resolved,
        )

        stop = asyncio.Event()
        task = asyncio.create_task(orchestrator.run(stop))
        try:
            yield
        finally:
            stop.set()
            await task
            await http.aclose()
            await engine.dispose()

    app = FastAPI(title="werft-manager", lifespan=lifespan)
    app.include_router(router)
    return app
