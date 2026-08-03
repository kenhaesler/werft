"""Composition root (SPEC §1 layering: the only module allowed to import
from every layer below it).

Lifespan wiring: the lifespan always builds one DB engine and one
`async_sessionmaker`, stashed on `app.state.session_factory` — the API
layer's `get_session` dependency (SPEC §9 operator surface) needs it
regardless of GitHub configuration, and `create_async_engine` is lazy (no
connection is opened until a route actually issues a query), so this costs
nothing when nothing queries it. The GitHub-backed orchestrator, on the
other hand, only starts when a GitHub App client id is configured
(`Settings.github_app_client_id`) — tests and local dev boot clean,
healthz-and-API-only, with no httpx client and no GitHub call ever made.
When creds are present, the lifespan reads the App's private key file,
builds one `httpx.AsyncClient`, one `AppAuth`, a manager-permission
`RepoOps` factory (`MANAGER_PERMISSIONS`) for day-to-day polling, and an
admin-permission `TransientAdminOps` factory for the one branch-protection
call onboarding/the doctrine-#1 flip need (SPEC §6.3) — then starts
`Orchestrator.run` as a background task. Shutdown sets the stop event and
awaits that task before disposing the engine/http client — both run
unconditionally even if awaiting the task itself raises (an
`ExceptionGroup` from a crashed loop must still leave a clean teardown) —
so a clean SIGTERM/lifespan-exit drains every in-flight unit of work
first.

The static bearer token guarding `/api/v1` (SPEC §9) is read once here,
synchronously, from its file mount (SPEC §10: secrets are file mounts,
never env) — before the app is even constructed, not inside the lifespan —
because `include_router`'s `dependencies=` is fixed at router-mount time.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from werft.api.auth import make_require_token
from werft.api.routes import api_router, healthz_router
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
    304.

    Because the cached object outlives the session that loaded the `Project`
    it was built from, **nothing cached here may hold an ORM reference.**
    `owner`/`repo`/`project_id` are read once, here, as plain values, and
    the token-provider closure captures those instead of the instance. The
    lazy read this replaces made the *first* unit that built the ops object
    decide that project's fate: `Orchestrator._run_unit` rolls back on any
    exception, SQLAlchemy expires every identity-mapped state on that
    rollback, the session closes — and every later request through the
    cached ops object then raised `DetachedInstanceError` from inside the
    token provider, before reaching HTTP. That project went dark for the
    life of the process while `/healthz` stayed green."""
    cache: dict[UUID, RepoOps] = {}

    def build(project: Project) -> RepoOps:
        project_id: UUID = project.id
        cached = cache.get(project_id)
        if cached is not None:
            return cached

        owner: str = project.github_owner
        repo: str = project.github_repo

        async def token_provider() -> str:
            token = await auth.token_for(owner, repo, permissions)
            return token.token

        client = GitHubClient(http, api_url=api_url, token_provider=token_provider)
        ops = RepoOps(client, owner, repo)
        cache[project_id] = ops
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
    token regardless of which wrapper instance made it.

    Same ORM-reference rule as `_ops_factory` all the same: `owner`/`repo`
    are read eagerly, here, into plain strings the wrapper then owns. It
    never holds the `Project` instance, so it cannot be poisoned by the
    session that loaded it rolling back."""

    def build(project: Project) -> TransientAdminOps:
        owner: str = project.github_owner
        repo: str = project.github_repo
        return TransientAdminOps(auth, http, api_url, owner, repo)

    return build


def _read_api_token(path: str) -> str | None:
    """Read the static bearer token once, at startup, from its file mount
    (SPEC §10: secrets are file mounts, never env). An empty path or a
    path that doesn't resolve to a file both mean "not configured" —
    `make_require_token` fails closed on `None` rather than treating a
    missing mount as "auth disabled"."""
    if not path:
        return None
    token_path = Path(path)
    if not token_path.is_file():
        return None
    return token_path.read_text().strip()


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()
    require_token = make_require_token(_read_api_token(resolved.api_token_file))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine_from_url(resolved.database_url)
        app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)

        if not resolved.github_app_client_id:
            # No GitHub App configured: the orchestrator never starts — no
            # httpx client, no GitHub call — but the API layer still needs
            # a session factory to serve /api/v1/runs. The engine above is
            # a lazy connection pool, so building it here opens no
            # connection until a route actually issues a query.
            try:
                yield
            finally:
                await engine.dispose()
            return

        private_key_pem = Path(resolved.github_app_private_key_file).read_text()
        http = httpx.AsyncClient()
        auth = AppAuth(
            http,
            client_id=resolved.github_app_client_id,
            private_key_pem=private_key_pem,
            api_url=resolved.github_api_url,
        )
        orchestrator = Orchestrator(
            app.state.session_factory,
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
    app.include_router(healthz_router)
    app.include_router(api_router, prefix="/api/v1", dependencies=[Depends(require_token)])
    return app
