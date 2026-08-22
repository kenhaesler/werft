import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import make_transient_to_detached
from sqlalchemy.orm.attributes import instance_state
from sqlalchemy.orm.exc import DetachedInstanceError
from structlog.testing import capture_logs

from werft.app import TransientAdminOps, _ops_factory, create_app
from werft.config.settings import Settings
from werft.db.models import Project, ProviderAccount
from werft.domain.errors import PermanentError
from werft.github.auth import ADMIN_PERMISSIONS, MANAGER_PERMISSIONS, AppAuth, InstallationToken
from werft.github.client import GitHubUnavailable
from werft.observe.alerts import NtfyAlertSink, NullAlertSink
from werft.orchestrator.finalize import NullQuota
from werft.orchestrator.loop import Orchestrator
from werft.quota.ledger import LedgerQuota


@pytest.fixture
def migrated_pg_url(pg_url: str) -> str:
    """Same migration-once-per-container recipe as
    `tests/integration/conftest.py::migrated_db`, duplicated locally: that
    fixture lives in `tests/integration/`'s own conftest, out of reach for
    `tests/unit/`. `command.upgrade` is idempotent, so a second call here
    (or from the integration suite, whichever runs first against the same
    session-scoped `pg_url`) is a no-op."""
    os.environ["WERFT_TEST_DATABASE_URL"] = pg_url
    command.upgrade(Config("alembic.ini"), "head")
    return pg_url


async def test_healthz_reports_ok() -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_create_app_without_creds_boots_clean_and_healthz_still_answers() -> None:
    """The composition root's own contract: no GitHub App creds configured
    (the default `Settings()`, as in tests/dev) means the lifespan never
    builds an httpx client or starts the orchestrator. The DB engine and
    session factory are still built unconditionally — the API layer
    (`/api/v1/runs`) needs `app.state.session_factory` regardless of GitHub
    config, and `create_async_engine` is lazy (no connection opens until a
    route actually issues a query), so an unconnected engine costs nothing
    at boot on this path. `/healthz` is entered through the real lifespan
    context (not a bare `ASGITransport`, which never runs lifespan at
    all)."""
    app = create_app(Settings())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert isinstance(app.state.quota, NullQuota)


def test_a_malformed_dispatch_config_fails_boot_loudly(tmp_path: Path) -> None:
    """D3: at startup the operator is watching. A manager that boots on a
    broken config parks every run at 03:00. `create_app` now parses the
    dispatch config before the app is even constructed, so this raises
    synchronously rather than only once the lifespan runs."""
    broken_file = tmp_path / "dispatch.json"
    broken_file.write_text("{not valid json")

    with pytest.raises(PermanentError):
        create_app(Settings(dispatch_config_file=str(broken_file)))


def test_a_runs_root_that_diverges_from_artifacts_root_warns() -> None:
    """Carried note 5: that divergence would silently send T8's collector to
    the wrong tree. `structlog` here is unconfigured (no `ProcessorFormatter`
    routing into stdlib `logging`), so `capture_logs` — not `caplog` — is
    the fixture that actually observes the event."""
    with capture_logs() as logs:
        create_app(Settings(runs_root="/srv/a", artifacts_root="/srv/b"))

    assert any(entry.get("event") == "app.runs_root_diverges_from_artifacts_root" for entry in logs)


async def test_the_provider_account_is_upserted_when_a_ceiling_is_configured(
    migrated_pg_url: str, tmp_path: Path
) -> None:
    """SPEC §7's one knob, reconciled at boot (plan decision D4): with GitHub
    creds configured and a ceiling set, the lifespan upserts a
    `provider_accounts` row and `app.state.quota` is a real `LedgerQuota`."""
    key_file = tmp_path / "app-key.pem"
    key_file.write_text(_generate_private_key_pem())
    label = f"acct-{uuid.uuid4().hex}"

    settings = Settings(
        database_url=migrated_pg_url,
        github_app_client_id="Iv1.test1234",
        github_app_private_key_file=str(key_file),
        github_api_url="https://github-api.invalid.test",
        quota_ceiling_seconds=18000,
        quota_account_label=label,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        assert isinstance(app.state.quota, LedgerQuota)

    engine = create_async_engine(migrated_pg_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            account = (
                await session.execute(select(ProviderAccount).where(ProviderAccount.label == label))
            ).scalar_one()
    finally:
        await engine.dispose()

    assert account.ceiling_seconds == 18000


async def test_no_ceiling_means_no_account_row_at_all(migrated_pg_url: str) -> None:
    """No invented ceilings (D4): `quota_ceiling_seconds` unset (the
    default) means no `provider_accounts` row is ever written for this
    label, and `app.state.quota` stays the `NullQuota` no-op."""
    label = f"no-ceiling-{uuid.uuid4().hex}"
    settings = Settings(database_url=migrated_pg_url, quota_account_label=label)
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        assert isinstance(app.state.quota, NullQuota)

    engine = create_async_engine(migrated_pg_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            rows = (
                await session.execute(select(ProviderAccount).where(ProviderAccount.label == label))
            ).all()
    finally:
        await engine.dispose()

    assert rows == []


def _generate_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


async def test_create_app_with_creds_starts_and_stops_orchestrator_within_five_seconds(
    tmp_path: Path, pg_url: str
) -> None:
    """With GitHub App creds configured, the lifespan builds the engine,
    httpx client, `AppAuth`, and `Orchestrator`, and starts `Orchestrator.run`
    as a background task on entry. `database_url` points at a real (if
    unmigrated) Postgres — `pg_url` — so the tick/poll loops' discovery
    queries hit a real connection rather than hanging on an unroutable
    host; any query failure (e.g. `projects` not existing yet) is caught
    and logged by the loop's own defensive wrapper, never raised here.

    Deliberately uses `Settings`' *real* poll cadences (15/60/30 s), not a
    1 s override: a bare-`sleep(interval_seconds)` implementation of
    `Orchestrator._loop` (rather than `wait_for(stop.wait(), ...)`) would
    still finish a 1 s-cadence test inside 5 s, silently hiding the bug this
    test exists to catch. The `asyncio.wait_for(..., timeout=5)` below is
    the entire load-bearing assertion — it raises on its own if `__aexit__`
    doesn't finish in time, so there is nothing left for a follow-up
    `assert elapsed < 5` to prove.
    """
    key_file = tmp_path / "app-key.pem"
    key_file.write_text(_generate_private_key_pem())

    settings = Settings(
        database_url=pg_url,
        github_app_client_id="Iv1.test1234",
        github_app_private_key_file=str(key_file),
        github_api_url="https://github-api.invalid.test",
    )
    app = create_app(settings)
    lifespan_cm = app.router.lifespan_context(app)

    await lifespan_cm.__aenter__()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/healthz")
        assert resp.status_code == 200
    finally:
        await asyncio.wait_for(lifespan_cm.__aexit__(None, None, None), timeout=5)


async def test_lifespan_publishes_the_orchestrators_own_merging_lock_on_app_state(
    tmp_path: Path, pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`app.state.merging_lock` must be the *same object* as the running
    `Orchestrator`'s `merging_lock`, not a lock of its own.

    The accept route's inline `advance_merging` kick (api/routes.py) runs in
    this process, on this event loop, concurrently with the poller's
    `_advance_all_merging` — two locks would serialize nothing, and the same
    `merging` row would still reach two concurrent `squash_merge` calls.
    Captures the instance the lifespan constructs (the same
    patch-`__init__`-and-record trick the ntfy drain test below uses), since
    the orchestrator is otherwise lifespan-local and unobservable."""
    key_file = tmp_path / "app-key.pem"
    key_file.write_text(_generate_private_key_pem())

    built: list[Orchestrator] = []
    original_init = Orchestrator.__init__

    def capturing_init(self: Orchestrator, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        built.append(self)

    monkeypatch.setattr(Orchestrator, "__init__", capturing_init)

    settings = Settings(
        database_url=pg_url,
        github_app_client_id="Iv1.test1234",
        github_app_private_key_file=str(key_file),
        github_api_url="https://github-api.invalid.test",
    )
    app = create_app(settings)
    # Before the lifespan: a placeholder, so the kick always has *a* lock.
    assert isinstance(app.state.merging_lock, asyncio.Lock)

    lifespan_cm = app.router.lifespan_context(app)
    await lifespan_cm.__aenter__()
    try:
        assert len(built) == 1
        assert app.state.merging_lock is built[0].merging_lock
    finally:
        await asyncio.wait_for(lifespan_cm.__aexit__(None, None, None), timeout=5)


async def test_default_docs_routes_are_not_served(tmp_path: Path) -> None:
    """FastAPI's `/docs`, `/redoc` and `/openapi.json` sit outside `/api/v1`,
    so the bearer dependency mounted on `api_router` never covers them: left
    on, they publish the entire operator surface — every mutation path and
    payload shape — to anything that reaches the port without a token, and
    the two HTML pages additionally fetch their bundles from a third-party
    CDN. Turned off at construction, they 404 like any unknown path.

    The schema itself is untouched: `app.openapi()` still builds it
    in-process, `HTTPBearer` security scheme included (api/auth.py relies on
    that documentation), which is the distinction being pinned here — the
    *route* is gone, not the schema."""
    token_file = tmp_path / "api-token"
    token_file.write_text("s3cr3t-token")
    app = create_app(Settings(api_token_file=str(token_file)))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            resp = await client.get(path)
            assert resp.status_code == 404, path

    schema = app.openapi()
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    assert "/api/v1/runs/{run_id}/review/accept" in schema["paths"]


# -- NtfyAlertSink wiring (Task 14) -------------------------------------------


async def test_create_app_without_ntfy_url_keeps_null_alert_sink() -> None:
    """`Settings.ntfy_url` empty (the default) means the composition root
    never touches ntfy — `app.state.alerts` stays the `NullAlertSink` set
    at construction time, before *and* during the lifespan."""
    app = create_app(Settings())
    assert isinstance(app.state.alerts, NullAlertSink)

    async with app.router.lifespan_context(app):
        assert isinstance(app.state.alerts, NullAlertSink)


async def test_lifespan_wires_ntfy_alert_sink_when_ntfy_url_configured() -> None:
    """`Settings.ntfy_url` configured swaps `app.state.alerts` for a real
    `NtfyAlertSink` on lifespan entry, and shutdown (`drain()` +
    `aclose()`) completes clean even with nothing ever sent — the wiring
    itself must not require a live ntfy server to boot or shut down."""
    settings = Settings(ntfy_url="https://ntfy.invalid.test", ntfy_topic="werft-test")
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        assert isinstance(app.state.alerts, NtfyAlertSink)


async def test_lifespan_with_github_and_ntfy_still_closes_resources_when_drain_hangs(
    tmp_path: Path, pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for review finding 1, exercised in the branch it actually
    bites: **both** GitHub creds and `ntfy_url` configured (unlike the two
    tests above, which each configure only one). A `drain()` that never
    returns — standing in for an unreachable/very slow ntfy host — must not
    prevent `ntfy_http.aclose()`, the GitHub `http.aclose()`, or
    `engine.dispose()` from running: `_drain_ntfy_bounded` (app.py) bounds
    the drain and the surrounding `finally` runs cleanup regardless.

    Patches `NtfyAlertSink.__init__` to capture the actual `httpx.AsyncClient`
    app.py builds for it (not observable through `app.state`, which only
    exposes the sink, not its client) and `NtfyAlertSink.drain` to hang
    forever. The outer `wait_for(..., timeout=5)` is the same load-bearing
    pattern `test_create_app_with_creds_starts_and_stops_orchestrator_within_five_seconds`
    already uses — it fails the test on its own if shutdown doesn't
    complete in time, which is exactly what an unfixed, unbounded
    `await drain()` would do."""
    key_file = tmp_path / "app-key.pem"
    key_file.write_text(_generate_private_key_pem())

    captured_http: list[httpx.AsyncClient] = []
    original_init = NtfyAlertSink.__init__

    def capturing_init(self: NtfyAlertSink, http: httpx.AsyncClient, **kwargs: Any) -> None:
        captured_http.append(http)
        original_init(self, http, **kwargs)

    async def hanging_drain(self: NtfyAlertSink) -> None:
        await asyncio.Event().wait()  # never set; simulates an unreachable host

    monkeypatch.setattr(NtfyAlertSink, "__init__", capturing_init)
    monkeypatch.setattr(NtfyAlertSink, "drain", hanging_drain)

    settings = Settings(
        database_url=pg_url,
        github_app_client_id="Iv1.test1234",
        github_app_private_key_file=str(key_file),
        github_api_url="https://github-api.invalid.test",
        ntfy_url="https://ntfy.invalid.test",
        ntfy_topic="werft-test",
    )
    app = create_app(settings)
    lifespan_cm = app.router.lifespan_context(app)

    await lifespan_cm.__aenter__()
    assert len(captured_http) == 1
    ntfy_http = captured_http[0]
    assert not ntfy_http.is_closed

    await asyncio.wait_for(lifespan_cm.__aexit__(None, None, None), timeout=5)

    assert ntfy_http.is_closed


# -- _ops_factory: ETag-cache-preserving memoization (fix 1) ------------------


async def test_ops_factory_memoizes_repo_ops_per_project_id() -> None:
    """Per-unit `GitHubClient` construction defeats the ETag cache: every
    poll used to call `ops_for(project)` fresh, discarding `GitHubClient`'s
    in-memory ETag store each time, so `If-None-Match` was never sent and
    every 60 s issue poll burned a real rate-limit unit instead of a free
    304 (SPEC §6.2). `_ops_factory`'s `build` must hand back the *same*
    `RepoOps` — and therefore the same underlying `GitHubClient` and its
    ETag store — for the same project id across repeated calls."""

    def unexpected_call(request: httpx.Request) -> httpx.Response:
        raise AssertionError("build() must never itself make a GitHub call")

    http = httpx.AsyncClient(transport=httpx.MockTransport(unexpected_call))
    auth = AppAuth(
        http, client_id="Iv1.test", private_key_pem="unused", api_url="https://api.github.test"
    )
    build = _ops_factory(auth, http, "https://api.github.test", MANAGER_PERMISSIONS)
    project = Project(id=uuid.uuid4(), slug="s", github_owner="acme", github_repo="widgets")

    first = build(project)
    second = build(project)

    assert first is second


class _RecordingAuth:
    """Duck-typed `AppAuth` that only records which `(owner, repo)` the
    token provider asked for — the whole point of the test below is that
    those two values are still readable at request time."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def token_for(
        self, owner: str, repo: str, permissions: dict[str, str], *, transient: bool = False
    ) -> InstallationToken:
        self.calls.append((owner, repo))
        return InstallationToken(
            token="ghs_live", expires_at=datetime.now(UTC) + timedelta(hours=1)
        )


def _poison_like_a_rolled_back_unit(project: Project) -> None:
    """Reproduce, without a database, exactly what `Orchestrator._run_unit`
    leaves behind when a unit raises: SQLAlchemy's non-nested
    `SessionTransaction.rollback()` calls `_restore_snapshot(dirty_only=False)`,
    which expires *every* state in the identity map, and the session then
    closes, leaving the instance detached as well. Any later attribute read
    on it raises `DetachedInstanceError` — there is no session left to
    refresh from. (`_expire` is private, but it is the one hook that
    reproduces the rollback's effect on a single instance in-process.)"""
    make_transient_to_detached(project)
    instance_state(project)._expire(project.__dict__, set())


async def test_memoized_ops_still_work_after_the_project_instance_is_expired_and_detached() -> None:
    """The memoized `RepoOps` outlives the session that loaded the `Project`
    it was built from, so its `token_provider` closure must capture
    `github_owner`/`github_repo` as plain strings at memoization time and
    hold no ORM reference at all.

    Reading them lazily inside the coroutine — the pre-fix shape — meant the
    *first* unit that built the ops object for a project also decided that
    project's fate: if that unit rolled back (a GitHub 503 during the first
    poll, a transient DB error), the captured instance was left expired and
    detached, and every subsequent request through the cached ops object
    raised `DetachedInstanceError` before it ever reached HTTP. The project
    went dark for the life of the process while `/healthz` stayed green.
    """
    auth = _RecordingAuth()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"object": {"sha": "sha-1"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    build = _ops_factory(auth, http, "https://api.github.test", MANAGER_PERMISSIONS)
    project = Project(id=uuid.uuid4(), slug="s", github_owner="acme", github_repo="widgets")
    ops = build(project)

    _poison_like_a_rolled_back_unit(project)
    with pytest.raises(DetachedInstanceError):  # sanity: the poisoning is real
        _ = project.github_owner

    assert await ops.get_ref_sha("unattended") == "sha-1"
    assert auth.calls == [("acme", "widgets")]
    assert seen[0].headers["authorization"] == "Bearer ghs_live"


# -- TransientAdminOps: mint -> protection call -> revoke (fix 4) ------------


class _SpyAuth:
    """Duck-typed `AppAuth`: records `token_for`/`revoke` call order and
    asserts every `token_for` call carries `transient=True` — the whole
    point of `TransientAdminOps` is that it never reads from or writes to
    `AppAuth`'s shared cache."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.token = "ghs_admin"

    async def token_for(
        self, owner: str, repo: str, permissions: dict[str, str], *, transient: bool = False
    ) -> InstallationToken:
        assert transient is True
        assert permissions == ADMIN_PERMISSIONS
        self.calls.append("mint")
        return InstallationToken(
            token=self.token, expires_at=datetime.now(UTC) + timedelta(hours=1)
        )

    async def revoke(self, token: str) -> bool:
        assert token == self.token
        self.calls.append("revoke")
        return True


async def test_transient_admin_ops_mints_calls_protection_then_revokes_in_order() -> None:
    auth = _SpyAuth()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {auth.token}"
        auth.calls.append("protection_call")
        return httpx.Response(200, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    admin_ops = TransientAdminOps(auth, http, "https://api.github.test", "acme", "widgets")

    await admin_ops.apply_partial_protection("unattended")

    assert auth.calls == ["mint", "protection_call", "revoke"]


async def test_transient_admin_ops_revokes_even_when_the_protection_call_raises() -> None:
    """The admin token must never be leaked past a failed protection call
    either — `revoke` runs in a `finally`, so an erroring GitHub response
    still gets the token revoked before the exception propagates."""
    auth = _SpyAuth()

    def handler(request: httpx.Request) -> httpx.Response:
        auth.calls.append("protection_call")
        return httpx.Response(500, json={"message": "boom"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    admin_ops = TransientAdminOps(auth, http, "https://api.github.test", "acme", "widgets")

    with pytest.raises(GitHubUnavailable):
        await admin_ops.apply_strict_protection("unattended")

    assert auth.calls == ["mint", "protection_call", "revoke"]
