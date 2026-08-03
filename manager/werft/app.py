"""Composition root (SPEC §1 layering: the only module allowed to import
from every layer below it).

Lifespan wiring: the GitHub-backed orchestrator only starts when a GitHub
App client id is configured (`Settings.github_app_client_id`) — tests and
local dev boot clean, healthz-only, with no engine, no httpx client, and no
GitHub call ever made. When creds are present, the lifespan reads the
App's private key file, builds one engine, one `httpx.AsyncClient`, one
`AppAuth`, a manager-permission `RepoOps` factory (`MANAGER_PERMISSIONS`)
for day-to-day polling, and an admin-permission `TransientAdminOps` factory
for the one branch-protection call onboarding/the doctrine-#1 flip need
(SPEC §6.3) — then starts `Orchestrator.run` as a background task. Shutdown
sets the stop event and awaits that task before disposing the engine/http
client — both run unconditionally even if awaiting the task itself raises
(an `ExceptionGroup` from a crashed loop must still leave a clean
teardown) — so a clean SIGTERM/lifespan-exit drains every in-flight unit
of work first.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

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
    GitHub call.

    Memoized per `project.id`: `GitHubClient` carries an in-memory ETag
    store that only pays off (SPEC §6.2: an authenticated 304 costs no
    rate-limit unit) if the *same* client instance survives from one poll
    to the next. A fresh `RepoOps`/`GitHubClient` per call — this
    function's pre-fix behavior — meant `If-None-Match` was never sent and
    every 60 s issue poll burned a real rate-limit unit instead of a free
    304."""
    cache: dict[UUID, RepoOps] = {}

    def build(project: Project) -> RepoOps:
        cached = cache.get(project.id)
        if cached is not None:
            return cached

        async def token_provider() -> str:
            token = await auth.token_for(project.github_owner, project.github_repo, permissions)
            return token.token

        client = GitHubClient(http, api_url=api_url, token_provider=token_provider)
        ops = RepoOps(client, project.github_owner, project.github_repo)
        cache[project.id] = ops
        return ops

    return build


def _fixed_token_provider(token: str) -> Callable[[], Awaitable[str]]:
    """A `GitHubClient` token provider that always returns the one token
    it was built with — `TransientAdminOps` mints exactly one token per
    call and never re-mints mid-call, so there is nothing for this
    provider to look up each time."""

    async def provider() -> str:
        return token

    return provider


class TransientAdminOps:
    """The only admin-permission ops object `flip_project`/`onboard_project`
    ever touch — duck-typed to expose just the two protection calls they
    make (`werft/github/auth.py`'s own doctrine: an admin-scoped token is
    minted transiently, for one protection call, and revoked immediately;
    `AppAuth.revoke` existed from day one but, before this fix, had no
    production caller).

    Each method mints a *fresh* installation token — bypassing `AppAuth`'s
    cache entirely (`token_for(..., transient=True)`), so this admin token
    is never handed to, or reused by, any other caller — builds a
    short-lived `GitHubClient`/`RepoOps` for that one call, and revokes the
    token in a `finally`, so no admin-scoped token outlives the single
    mutating call that needed it, success or failure alike."""

    def __init__(
        self, auth: AppAuth, http: httpx.AsyncClient, api_url: str, owner: str, repo: str
    ) -> None:
        self._auth = auth
        self._http = http
        self._api_url = api_url
        self._owner = owner
        self._repo = repo

    async def _call(self, method_name: str, branch: str) -> None:
        token = await self._auth.token_for(
            self._owner, self._repo, ADMIN_PERMISSIONS, transient=True
        )
        client = GitHubClient(
            self._http, api_url=self._api_url, token_provider=_fixed_token_provider(token.token)
        )
        ops = RepoOps(client, self._owner, self._repo)
        try:
            await getattr(ops, method_name)(branch)
        finally:
            await self._auth.revoke(token.token)

    async def apply_strict_protection(self, branch: str) -> None:
        await self._call("apply_strict_protection", branch)

    async def apply_partial_protection(self, branch: str) -> None:
        await self._call("apply_partial_protection", branch)


def _admin_ops_factory(
    auth: AppAuth, http: httpx.AsyncClient, api_url: str
) -> Callable[[Project], Any]:
    """The admin factory: one `TransientAdminOps` per project. Unlike
    `_ops_factory`'s manager-permission clients, there is nothing here
    worth memoizing — `TransientAdminOps` holds no persistent client or
    ETag cache of its own; every one of its calls mints and revokes its own
    token regardless of which wrapper instance made it."""

    def build(project: Project) -> TransientAdminOps:
        return TransientAdminOps(auth, http, api_url, project.github_owner, project.github_repo)

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
            _admin_ops_factory(auth, http, resolved.github_api_url),
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
            try:
                await task
            finally:
                # Always run, even if awaiting the orchestrator's task itself
                # raised (e.g. an ExceptionGroup surfacing out of a crashed
                # loop) — a teardown that skips these on that path leaks the
                # httpx client's connections and the engine's pool.
                await http.aclose()
                await engine.dispose()

    app = FastAPI(title="werft-manager", lifespan=lifespan)
    app.include_router(router)
    return app
