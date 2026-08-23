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

The orchestrator's `merging_lock` is published on `app.state` in that same
GitHub branch: the accept route's inline `advance_merging` kick runs on
this one event loop, alongside the poller's own `_advance_all_merging`, and
the two must serialize on the *same* lock instance or the same `merging`
row reaches two concurrent `squash_merge` calls (`orchestrator/loop.py`
documents that race in full). `create_app` seeds a placeholder lock for the
paths where no orchestrator exists at all.

`NtfyAlertSink` (SPEC §9.5) is wired independently of the GitHub branch —
`app.state.alerts` is a consumer of both the orchestrator and the API
layer's mutation routes (routes.py's accept endpoint), so its httpx client
is built and torn down in the lifespan regardless of whether GitHub creds
are configured, with `Settings.ntfy_url` empty meaning "keep the
`NullAlertSink` default". Shutdown calls `drain()` before closing that
client — the same bounded-teardown discipline as the GitHub httpx client.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import structlog
from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import async_sessionmaker

from werft.api.auth import make_require_token
from werft.api.routes import api_router, healthz_router
from werft.config.dispatch import DispatchConfigCache, load_dispatch_config
from werft.config.settings import Settings
from werft.db.engine import create_engine_from_url
from werft.db.models import Project
from werft.domain.db_url import apply_password_file
from werft.domain.errors import PermanentError
from werft.github.auth import ADMIN_PERMISSIONS, MANAGER_PERMISSIONS, AppAuth
from werft.github.client import GitHubClient
from werft.github.ops import RepoOps
from werft.observe.alerts import NtfyAlertSink, NullAlertSink
from werft.orchestrator.finalize import NullQuota, QuotaPort
from werft.orchestrator.loop import DispatchServices, Orchestrator
from werft.providers.claude import ClaudeSpec
from werft.quota.accounts import ensure_provider_account
from werft.quota.ledger import LedgerQuota
from werft.runner.docker_api import DockerClient

logger = structlog.get_logger(__name__)

#: The most egress slots the addressing scheme can express: a slot's subnet is
#: `<egress_subnet_prefix>.<slot>.0/24`, so the slot number is one octet
#: (`egress_admin._validate_slot` accepts 0..255).
EGRESS_SLOT_CEILING = 256

#: Cap on `NtfyAlertSink.drain()` at shutdown. `NtfyAlertSink.drain()`
#: already never raises from an individual spawned task's own exception
#: (alerts.py: `gather(..., return_exceptions=True)`) — this instead bounds
#: the one failure mode that isn't a task exception at all: a slow or
#: unreachable ntfy host simply taking too long. Comfortably inside the 5 s
#: whole-lifespan-exit budget `test_app.py` pins, leaving headroom for the
#: orchestrator task join, `http.aclose()`, and `engine.dispose()` that run
#: after it in the same teardown.
_NTFY_DRAIN_TIMEOUT = 2.0


async def _drain_ntfy_bounded(alerts: NtfyAlertSink) -> None:
    """`await alerts.drain()`, but never let it run past `_NTFY_DRAIN_TIMEOUT`
    — the caller's `finally` (aclose/dispose) must run either way, so a
    timeout here is logged and swallowed rather than left to propagate and
    skip that cleanup. A genuine external cancellation of the *caller* (not
    one this function's own timeout triggered) is not swallowed — it still
    propagates after the caller's `finally` runs, exactly like the
    orchestrator task join a few lines below in `lifespan` already treats
    its own exceptions."""
    try:
        await asyncio.wait_for(alerts.drain(), timeout=_NTFY_DRAIN_TIMEOUT)
    except TimeoutError:
        logger.warning("ntfy.drain_timed_out", timeout=_NTFY_DRAIN_TIMEOUT)


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


def _read_token_file(path: str) -> str | None:
    """Read a secret token once, at startup, from its file mount (SPEC §10:
    secrets are file mounts, never env). An empty path or a path that
    doesn't resolve to a file both mean "not configured" (`None`) —
    callers decide what that means: `make_require_token` fails closed on
    `None` rather than treating a missing mount as "auth disabled";
    `NtfyAlertSink` just omits the `Authorization` header."""
    if not path:
        return None
    token_path = Path(path)
    if not token_path.is_file():
        return None
    return token_path.read_text().strip()


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()

    # SPEC §10: secrets are file mounts, never env. Resolved synchronously,
    # here, alongside the other boot-time guards below (D3: the operator is
    # watching at startup) — a `WERFT_DATABASE_PASSWORD_FILE` that's set but
    # unreadable is a `PermanentError` now, not a confusing failure on the
    # engine's first query once the lifespan is already running.
    database_url = apply_password_file(resolved.database_url, resolved.database_password_file)

    # Fail boot loudly on a broken dispatch config (plan decision D3): the
    # operator is watching at startup, and a manager that boots on a broken
    # file parks every run at 03:00. Mid-flight the rule is the opposite —
    # `DispatchConfigCache` keeps the last good value.
    load_dispatch_config(resolved.dispatch_config_file)
    if resolved.runs_root != resolved.artifacts_root:
        logger.warning(
            "app.runs_root_diverges_from_artifacts_root",
            runs_root=resolved.runs_root,
            artifacts_root=resolved.artifacts_root,
        )

    # T9 boot guard (SPEC §4.5/§10): with egress plumbing on
    # (`egress_slot_count > 0`), a claim that can never find a free slot
    # would otherwise retry forever, unattended, at 03:00 — the same D3
    # "operator is watching at startup" rationale as the dispatch-config
    # check above. `egress_slot_count == 0` means the plumbing is off
    # entirely (today's Internal-only network), so `max_concurrent_runs`
    # is unconstrained by it.
    # Upper bound, same D3 rationale: a slot's third octet is the slot number
    # (`egress_admin.slot_subnet` -> `<prefix>.<slot>.0/24`, `_validate_slot`
    # accepts 0..255), so 256 is the ceiling the addressing scheme can express.
    # Above it, `_claim_slot` walks off the end of the octet and every claim
    # past 255 dies on a `ValueError` mid-dispatch instead of at boot, where
    # the operator is watching.
    if resolved.egress_slot_count > EGRESS_SLOT_CEILING:
        raise PermanentError(
            f"egress_slot_count ({resolved.egress_slot_count}) exceeds the "
            f"{EGRESS_SLOT_CEILING}-slot ceiling: a slot's subnet is "
            "<egress_subnet_prefix>.<slot>.0/24, so slot numbers must fit in "
            "one octet (0..255). Lower WERFT_EGRESS_SLOT_COUNT."
        )
    if resolved.egress_slot_count > 0 and resolved.max_concurrent_runs > resolved.egress_slot_count:
        raise PermanentError(
            "max_concurrent_runs "
            f"({resolved.max_concurrent_runs}) exceeds egress_slot_count "
            f"({resolved.egress_slot_count}): claims above the slot count "
            "could never find a free slot and would retry forever. Raise "
            "egress_slot_count or lower max_concurrent_runs."
        )
    # SPEC §8: egress active but the evidence seam (squid/dns-guard query
    # logs) still dormant is legal — T9 provisions the services and sets
    # these paths later (plan D7: empty means "not deployed", collection is
    # a silent no-op) — but the operator should know at a glance rather than
    # discover it during an incident review.
    if resolved.egress_slot_count > 0 and (
        not resolved.squid_access_log or not resolved.dns_guard_query_log
    ):
        logger.warning(
            "app.egress_active_evidence_seam_dormant",
            egress_slot_count=resolved.egress_slot_count,
            squid_access_log=resolved.squid_access_log,
            dns_guard_query_log=resolved.dns_guard_query_log,
        )

    require_token = make_require_token(_read_token_file(resolved.api_token_file))
    ntfy_token = _read_token_file(resolved.ntfy_token_file)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine_from_url(database_url)
        app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)

        # `NtfyAlertSink` is independent of GitHub config — the API layer's
        # accept route (routes.py) fires `alerts.run_parked` even when no
        # orchestrator is running — so its httpx client is owned here,
        # before the GitHub branch, and closed in both branches below.
        ntfy_http: httpx.AsyncClient | None = None
        if resolved.ntfy_url:
            ntfy_http = httpx.AsyncClient()
            app.state.alerts = NtfyAlertSink(
                ntfy_http, url=resolved.ntfy_url, topic=resolved.ntfy_topic, token=ntfy_token
            )

        if not resolved.github_app_client_id:
            # No GitHub App configured: the orchestrator never starts — no
            # GitHub httpx client, no GitHub call — but the API layer still
            # needs a session factory to serve /api/v1/runs. The engine
            # above is a lazy connection pool, so building it here opens no
            # connection until a route actually issues a query.
            #
            # `app.state.quota` is `NullQuota()` on this path regardless of
            # `quota_ceiling_seconds`: dispatch never runs without an
            # orchestrator, and no orchestrator exists here — but the cancel
            # route still needs a port to call `release` on.
            app.state.quota = NullQuota()
            try:
                yield
            finally:
                try:
                    if ntfy_http is not None:
                        await _drain_ntfy_bounded(app.state.alerts)
                finally:
                    if ntfy_http is not None:
                        await ntfy_http.aclose()
                    await engine.dispose()
            return

        # Everything from here down can raise before the `try: yield` below
        # is ever reached (a missing key file, a DB error out of
        # `ensure_provider_account`, a `docker.negotiate()` that can't reach
        # the socket) — and by that point `engine`, and possibly `ntfy_http`,
        # are already open. Without this `try`, that failure would propagate
        # straight out of `lifespan` and leak both: the `finally` chain that
        # closes them lives inside the `try: yield` block below, which a
        # startup failure never enters. `http`/`docker` are declared here,
        # ahead of assignment, purely so the `except` can tell "never
        # opened" apart from "opened, now must be closed" for each.
        http: httpx.AsyncClient | None = None
        docker: DockerClient | None = None
        try:
            private_key_pem = Path(resolved.github_app_private_key_file).read_text()
            http = httpx.AsyncClient()
            auth = AppAuth(
                http,
                client_id=resolved.github_app_client_id,
                private_key_pem=private_key_pem,
                api_url=resolved.github_api_url,
            )
            # The same two factories the orchestrator's poll loop uses are
            # also what the operator API's mutation routes need (onboard,
            # manual flip, accept's best-effort inline `advance_merging`) —
            # one composition root, one set of factories, published on
            # `app.state` rather than each route minting its own
            # `GitHubClient`/`AppAuth`.
            app.state.ops_for = _ops_factory(
                auth, http, resolved.github_api_url, MANAGER_PERMISSIONS
            )
            app.state.admin_ops_for = _admin_ops_factory(auth, http, resolved.github_api_url)

            # SPEC §7's one knob, reconciled at boot (plan decision D4). `DO
            # UPDATE` so config + restart is the whole operator surface —
            # there is no seed script, no migration and no endpoint, because
            # SPEC §9 closes the write set and a migration would bake a
            # fabricated ceiling into every deploy. With the ceiling unset,
            # no row exists and nothing dispatches: no invented ceilings,
            # and no run is parked for it.
            quota: QuotaPort = NullQuota()
            if resolved.quota_ceiling_seconds > 0:
                quota = LedgerQuota(
                    provider=resolved.quota_provider,
                    label=resolved.quota_account_label,
                    fallback_seconds=resolved.quota_block_fallback_seconds,
                    typical_reservation_seconds=resolved.typical_reservation_seconds,
                )
                async with app.state.session_factory() as session, session.begin():
                    await ensure_provider_account(
                        session,
                        provider=resolved.quota_provider,
                        label=resolved.quota_account_label,
                        ceiling_seconds=resolved.quota_ceiling_seconds,
                        rolling_window_hours=resolved.quota_rolling_window_hours,
                        window_cap_runs=resolved.quota_window_cap_runs,
                        provider_window_capacity_seconds=resolved.quota_window_capacity_seconds,
                    )
            app.state.quota = quota  # the cancel route's true-up (D10)

            # Review finding 1 (Task 17 fix round 1): the brief's literal
            # three-way gate — dispatch config + credential file + GitHub
            # creds — omitted the quota ceiling. SPEC §7 ("no reservation,
            # no claim") makes quota load-bearing for dispatch: with the
            # ceiling at its 0 default, `quota` above is `NullQuota()`, and
            # `claim_next` calls `lock_and_resolve`/`next_attempt_no`/
            # `reserve` — none of which `NullQuota` implements — so the
            # first `_sweep_dispatch` tick would die with `AttributeError`.
            # A ceiling of 0 therefore leaves the dispatch plane dark
            # (`dispatch=None`) rather than failing boot: the operator may
            # be staging the other three settings ahead of the ceiling, and
            # a boot failure here would be a worse surprise than a quiet,
            # loudly-logged no-op.
            dispatch_services = None
            if resolved.dispatch_config_file and resolved.claude_credential_file:
                if resolved.quota_ceiling_seconds > 0:
                    docker = DockerClient(resolved.docker_url)
                    await docker.negotiate()
                    dispatch_services = DispatchServices(
                        docker=docker,
                        auth=auth,
                        spec=ClaudeSpec(),
                        config=DispatchConfigCache(resolved.dispatch_config_file),
                        quota=quota,
                    )
                else:
                    logger.warning(
                        "app.dispatch_disabled_no_quota_ceiling",
                        dispatch_config_file=resolved.dispatch_config_file,
                        claude_credential_file=resolved.claude_credential_file,
                        reason="quota_ceiling_seconds is 0: SPEC §7 requires a "
                        "configured ceiling before the dispatch plane may claim "
                        "runs (no reservation, no claim). Set "
                        "WERFT_QUOTA_CEILING_SECONDS to enable dispatch.",
                    )
            orchestrator = Orchestrator(
                app.state.session_factory,
                app.state.ops_for,
                app.state.admin_ops_for,
                alerts=app.state.alerts,
                quota=quota,
                settings=resolved,
                dispatch=dispatch_services,
            )
            # The orchestrator's own merging lock, published for the accept
            # route's inline `advance_merging` kick (api/routes.py). This
            # branch is the only place an orchestrator exists at all — and
            # it is exactly the branch that sets `ops_for`, the kick's own
            # guard — so whenever the kick runs, the poller is running too
            # and the two genuinely share this instance. The placeholder
            # lock set in `create_app` below is only ever the one the kick
            # takes when no orchestrator exists to contend with it.
            app.state.merging_lock = orchestrator.merging_lock
        except Exception:
            # Same close order the normal teardown below uses
            # (ntfy -> http -> docker -> engine), just without the bounded
            # drain: nothing has been queued on `ntfy_http` yet at this
            # point in startup, so there is nothing to drain, only to close.
            try:
                if ntfy_http is not None:
                    await ntfy_http.aclose()
            finally:
                try:
                    if http is not None:
                        await http.aclose()
                finally:
                    try:
                        if docker is not None:
                            await docker.aclose()
                    finally:
                        await engine.dispose()
            raise

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
                # httpx client's connections and the engine's pool. Same
                # discipline for the ntfy drain, nested one level further:
                # `_drain_ntfy_bounded` is itself bounded (never blocks past
                # `_NTFY_DRAIN_TIMEOUT`), and this `finally` guarantees
                # aclose()/dispose() run even on the timeout it swallows.
                try:
                    if ntfy_http is not None:
                        await _drain_ntfy_bounded(app.state.alerts)
                finally:
                    if ntfy_http is not None:
                        await ntfy_http.aclose()
                    await http.aclose()
                    if docker is not None:
                        await docker.aclose()
                    await engine.dispose()

    # FastAPI's three default documentation routes are turned off, not
    # merely left unlinked: they sit outside `/api/v1`, so the bearer
    # dependency mounted on `api_router` never applies to them, and they
    # would publish the whole operator surface — every mutation path and
    # payload shape — to anything that can reach the port without a token.
    # `/docs` and `/redoc` additionally pull Swagger UI/ReDoc bundles from a
    # third-party CDN, an outbound fetch this Tailscale-only appliance has
    # no business making. `app.openapi()` still builds the schema in-process
    # for anyone who wants it (tests do); it is only the unauthenticated
    # HTTP surface that is gone.
    app = FastAPI(
        title="werft-manager",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Set before the lifespan ever runs (like `require_token` above) so the
    # operator API's mutation routes have a well-defined GitHub-unconfigured
    # default (`None`/`None`/`NullAlertSink()`) even in tests that override
    # `get_session` and never enter `lifespan_context` at all. When GitHub
    # creds are configured, the lifespan overwrites `ops_for`/`admin_ops_for`
    # with the real factories; `alerts` is deliberately the same instance the
    # orchestrator itself uses — the lifespan swaps it for `NtfyAlertSink`,
    # once, for both consumers, when `ntfy_url` is configured.
    app.state.ops_for = None
    app.state.admin_ops_for = None
    app.state.alerts = NullAlertSink()
    # Same reasoning as `alerts` above: the cancel route (api/routes.py)
    # reads `request.app.state.quota` unconditionally, including in tests
    # that never enter `lifespan_context`. Both lifespan branches overwrite
    # this — the no-GitHub branch with a fresh `NullQuota()`, the
    # GitHub-configured branch with the reconciled `LedgerQuota`/`NullQuota`
    # pair described above.
    app.state.quota = NullQuota()
    # Placeholder for the orchestrator's own lock (the lifespan's GitHub
    # branch overwrites it with `orchestrator.merging_lock`), so the accept
    # route's kick always has a lock to take — including in tests that never
    # enter the lifespan, and on a GitHub-unconfigured boot where no
    # orchestrator exists and the kick is skipped anyway. Constructing an
    # `asyncio.Lock` outside a running loop is safe: it binds to a loop on
    # first acquisition, and this process only ever has one.
    app.state.merging_lock = asyncio.Lock()
    # Read straight off `resolved` rather than deferred to the lifespan: the
    # artifact-download route (api/routes.py) needs it on every request, and
    # tests that override `get_session` and never enter `lifespan_context`
    # (test_api_runs.py's style) still need a well-defined value.
    app.state.artifacts_root = resolved.artifacts_root
    app.include_router(healthz_router)
    app.include_router(api_router, prefix="/api/v1", dependencies=[Depends(require_token)])

    # B7: the built dashboard is served, conditionally, at `/ui` —
    # `StaticFiles` requires its directory to exist at construction time
    # (unlike everything else in this function, which is happy to be
    # configured against a not-yet-real path), so the mount only happens
    # when `dashboard_dist` names a directory that's actually there. A
    # manager deployed before the dashboard is built, or with the setting
    # left unset, still boots — API-only, `/` unmounted (404) — rather than
    # crashing at startup.
    if resolved.dashboard_dist and Path(resolved.dashboard_dist).is_dir():
        app.mount("/ui", StaticFiles(directory=resolved.dashboard_dist, html=True), name="ui")

        @app.get("/")
        async def _redirect_root_to_ui() -> RedirectResponse:
            return RedirectResponse(url="/ui/", status_code=307)

    return app
