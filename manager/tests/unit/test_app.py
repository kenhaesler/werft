import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.orm import make_transient_to_detached
from sqlalchemy.orm.attributes import instance_state
from sqlalchemy.orm.exc import DetachedInstanceError

from werft.app import TransientAdminOps, _ops_factory, create_app
from werft.config.settings import Settings
from werft.db.models import Project
from werft.github.auth import ADMIN_PERMISSIONS, MANAGER_PERMISSIONS, AppAuth, InstallationToken
from werft.github.client import GitHubUnavailable


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
