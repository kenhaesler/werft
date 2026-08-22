"""`/api/v1` mutation endpoints against a real DB (SPEC §9's closed write
set, Task 12/B3): review accept/reject, run cancel/requeue, project
onboard, and the manual lifecycle flip.

App wiring follows `test_api_runs.py`'s style (`get_session` overridden with
the `db_session` fixture's session directly, so nothing here ever enters
`create_app`'s lifespan). Because these tests never enter the lifespan,
`app.state.ops_for`/`admin_ops_for`/`alerts` — normally built there — are
set directly on the app object after `create_app()` returns, exactly as
`app.py` initializes them to (`None`/`None`/`NullAlertSink()`) before the
lifespan ever runs, so a test that never overrides them exercises the real
GitHub-unconfigured defaults.

Fakes for the GitHub-touching endpoints (onboard, flip, and accept's
best-effort inline `advance_merging`) are duck-typed call-log fakes, the
same style `test_onboard.py`/`test_ci_watch.py` use — including
`FakeAdminOps`'s `strict_calls`/`partial_calls` split, which is what proves
"the right protection body per direction" at this layer (flip_project never
exposes a raw HTTP body to a caller; the exact-argument call log is the
API-level equivalent of `test_github_ops.py`'s verbatim dict assertions).
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from werft.api.routes import get_session
from werft.app import create_app
from werft.config.settings import Settings
from werft.db.models import BacklogItem, Project, QuotaLedgerEntry, Run, RunAttempt
from werft.github.ops import READY_LABEL, PullRequest
from werft.observe.alerts import NullAlertSink
from werft.orchestrator.onboard import duplicate_project_message
from werft.quota.ledger import LedgerQuota

# -- seeding ------------------------------------------------------------


async def seed_project(session, *, owner: str = "acme", repo: str | None = None) -> Project:
    tag = uuid.uuid4().hex[:8]
    pid = (
        await session.execute(
            text(
                "INSERT INTO projects (slug, github_owner, github_repo) "
                "VALUES (:slug, :owner, :repo) RETURNING id"
            ),
            {"slug": f"p-{tag}", "owner": owner, "repo": repo or f"r-{tag}"},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(Project, pid)


async def seed_backlog_item(
    session, project: Project, number: int, *, title: str = "an issue"
) -> BacklogItem:
    bid = (
        await session.execute(
            text(
                "INSERT INTO backlog_items "
                "(project_id, github_issue_number, title, github_updated_at) "
                "VALUES (:p, :n, :t, now()) RETURNING id"
            ),
            {"p": project.id, "n": number, "t": title},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(BacklogItem, bid)


async def seed_run(
    session,
    project: Project,
    item: BacklogItem,
    *,
    status: str = "queued",
    attempt_count: int = 0,
    max_attempts: int = 3,
    pr_number: int | None = None,
    next_attempt_at_offset_seconds: float | None = None,
    parked_reason: str | None = None,
) -> Run:
    rid = (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status, attempt_count, "
                "max_attempts, pr_number, parked_reason, next_attempt_at) "
                "VALUES (:p, :b, :s, :ac, :ma, :pr, :parked_reason, "
                "CASE WHEN CAST(:offset AS double precision) IS NULL THEN now() "
                "ELSE now() - make_interval(secs => CAST(:offset AS double precision)) END) "
                "RETURNING id"
            ),
            {
                "p": project.id,
                "b": item.id,
                "s": status,
                "ac": attempt_count,
                "ma": max_attempts,
                "pr": pr_number,
                "parked_reason": parked_reason,
                "offset": next_attempt_at_offset_seconds,
            },
        )
    ).scalar_one()
    await session.commit()
    return await session.get(Run, rid)


# -- fakes / spies --------------------------------------------------------


class FakeMergeOps:
    """Duck-typed `RepoOps` for `advance_merging`'s bootstrap merge path —
    enough to drive one `awaiting_review -> merging -> merged` accept in a
    single tick (a `clean`, non-`behind` PR, so `_advance_bootstrap_merging`
    goes straight to `squash_merge`)."""

    def __init__(self, pr: PullRequest, *, merge_commit_sha: str = "merge-sha-1") -> None:
        self.pr = pr
        self.merge_commit_sha = merge_commit_sha
        self.squash_merge_calls: list[tuple[int, str, str]] = []
        self.remove_label_calls: list[tuple[int, str]] = []
        self.delete_ref_calls: list[str] = []
        self.update_branch_calls: list[tuple[int, str]] = []

    async def get_pr(self, number: int) -> PullRequest:
        return self.pr

    async def squash_merge(self, number: int, head_sha: str, commit_title: str) -> str:
        self.squash_merge_calls.append((number, head_sha, commit_title))
        return self.merge_commit_sha

    async def remove_label(self, issue_number: int, name: str) -> None:
        self.remove_label_calls.append((issue_number, name))

    async def delete_ref(self, branch: str) -> None:
        self.delete_ref_calls.append(branch)

    async def update_branch(self, number: int, expected_head_sha: str) -> None:
        self.update_branch_calls.append((number, expected_head_sha))


class FakeRepoOps:
    """Duck-typed manager-permission `RepoOps` for `onboard_project`, same
    shape `test_onboard.py`'s own `FakeRepoOps` uses."""

    def __init__(self, *, main_sha: str | None = "main-sha-abc123") -> None:
        self.main_sha = main_sha
        self.get_ref_sha_calls: list[str] = []
        self.ensure_branch_calls: list[tuple[str, str]] = []
        self.ensure_label_calls: list[tuple[str, str]] = []

    async def get_ref_sha(self, branch: str) -> str | None:
        self.get_ref_sha_calls.append(branch)
        return self.main_sha

    async def ensure_branch(self, branch: str, from_sha: str) -> str:
        self.ensure_branch_calls.append((branch, from_sha))
        return from_sha

    async def ensure_label(self, name: str, color: str) -> None:
        self.ensure_label_calls.append((name, color))


class FakeAdminOps:
    """Duck-typed admin-permission `RepoOps` protection calls only — the
    seam both `onboard_project` (partial only) and `flip_project` (either
    direction) touch. `strict_calls`/`partial_calls` naming matches
    `test_ci_watch.py`'s own `FakeAdminOps`."""

    def __init__(self) -> None:
        self.strict_calls: list[str] = []
        self.partial_calls: list[str] = []

    async def apply_strict_protection(self, branch: str) -> None:
        self.strict_calls.append(branch)

    async def apply_partial_protection(self, branch: str) -> None:
        self.partial_calls.append(branch)


class SpyAlertSink(NullAlertSink):
    def __init__(self) -> None:
        self.run_parked_calls: list[tuple[str, uuid.UUID, str]] = []
        self.project_flipped_calls: list[str] = []

    async def run_parked(self, project_slug, run_id, reason) -> None:
        self.run_parked_calls.append((project_slug, run_id, reason))

    async def project_flipped(self, project_slug) -> None:
        self.project_flipped_calls.append(project_slug)


def ops_for_factory(ops):
    return lambda project: ops


def admin_ops_for_factory(admin_ops):
    return lambda project: admin_ops


# -- app wiring -----------------------------------------------------------

TOKEN = "s3cr3t-token"


def make_client_app(
    db_session: AsyncSession,
    *,
    token_file: str = "",
    ops_for=None,
    admin_ops_for=None,
    alerts=None,
):
    app = create_app(Settings(api_token_file=token_file))

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _override_get_session
    if ops_for is not None:
        app.state.ops_for = ops_for
    if admin_ops_for is not None:
        app.state.admin_ops_for = admin_ops_for
    if alerts is not None:
        app.state.alerts = alerts
    # These tests never enter `create_app`'s lifespan (same rationale as
    # `ops_for`/`admin_ops_for`/`alerts` above), so `app.state.quota` stays
    # the synchronous default `create_app` sets before the lifespan ever
    # runs. `cancel_run`'s true-up (Task 17) needs a real port here — the
    # account/label are irrelevant to `LedgerQuota.release`, which resolves
    # the run's own newest open ledger row rather than reading the account.
    app.state.quota = LedgerQuota()
    return app


@pytest.fixture
def token_file(tmp_path) -> str:
    path = tmp_path / "api-token"
    path.write_text(TOKEN)
    return str(path)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


# -- review accept ----------------------------------------------------------


async def test_accept_happy_path_drives_fake_ops_merge_to_merged(
    db_session, token_file, auth_headers
) -> None:
    project = await seed_project(db_session, owner="acme", repo="widgets")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="awaiting_review", pr_number=7)

    pr = PullRequest(
        number=7,
        state="open",
        merged=False,
        head_ref=f"werft/run-{run.id}",
        head_sha="head-sha-1",
        base_ref=project.unattended_branch,
        mergeable=True,
        mergeable_state="clean",
        html_url="https://github.com/acme/widgets/pull/7",
    )
    ops = FakeMergeOps(pr)

    app = make_client_app(db_session, token_file=token_file, ops_for=ops_for_factory(ops))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/runs/{run.id}/review/accept", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "merged"
    assert body["id"] == str(run.id)

    assert ops.squash_merge_calls == [(7, "head-sha-1", f"werft: {project.slug} run {run.id}")]
    assert ops.remove_label_calls == [(1, READY_LABEL)]
    assert ops.delete_ref_calls == [f"werft/run-{run.id}"]

    fresh = await db_session.get(Run, run.id, populate_existing=True)
    assert fresh.status == "merged"
    assert fresh.merge_commit_sha == "merge-sha-1"


async def test_accept_on_queued_run_is_409_state_untouched(
    db_session, token_file, auth_headers
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="queued")

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/runs/{run.id}/review/accept", headers=auth_headers)

    assert resp.status_code == 409
    fresh = await db_session.get(Run, run.id, populate_existing=True)
    assert fresh.status == "queued"
    assert fresh.version == 0


async def test_accept_without_ops_configured_still_advances_state_no_crash(
    db_session, token_file, auth_headers
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="awaiting_review", pr_number=7)

    app = make_client_app(db_session, token_file=token_file)  # ops_for stays None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/runs/{run.id}/review/accept", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "merging"

    fresh = await db_session.get(Run, run.id, populate_existing=True)
    assert fresh.status == "merging"


async def test_accept_kick_holds_the_shared_merging_lock_while_it_talks_to_github(
    db_session, token_file, auth_headers
) -> None:
    """The inline kick must run *inside* `app.state.merging_lock` — the same
    lock instance `Orchestrator._advance_all_merging` holds across discovery
    + advance (`orchestrator/loop.py`).

    The accept CAS commits `merging` before the kick starts, so the poller's
    discovery query (`WHERE status = 'merging'`, same process, same event
    loop) can already see this row while the kick sits in `get_pr`/
    `squash_merge`. Unlocked, both callers reach `ops.squash_merge` for one
    row: the winner merges the PR, the loser gets GitHub's 405 ->
    `MergeBlocked` and parks the run, so a merged PR ends up recorded
    `parked/merge_blocked` with a NULL `merge_commit_sha` and a spurious
    operator alert.

    Asserted at the seam that cannot be faked: the fake ops records
    `merging_lock.locked()` at the moment of its first GitHub call. Drop the
    `async with` from the route and the recorded value is `False`."""
    project = await seed_project(db_session, owner="acme", repo="widgets")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="awaiting_review", pr_number=7)

    pr = PullRequest(
        number=7,
        state="open",
        merged=False,
        head_ref=f"werft/run-{run.id}",
        head_sha="head-sha-1",
        base_ref=project.unattended_branch,
        mergeable=True,
        mergeable_state="clean",
        html_url="https://github.com/acme/widgets/pull/7",
    )

    app = make_client_app(db_session, token_file=token_file)
    lock = app.state.merging_lock

    class LockObservingOps(FakeMergeOps):
        def __init__(self) -> None:
            super().__init__(pr)
            self.lock_held_at_github_call: list[bool] = []

        async def get_pr(self, number: int) -> PullRequest:
            self.lock_held_at_github_call.append(lock.locked())
            return await super().get_pr(number)

    ops = LockObservingOps()
    app.state.ops_for = ops_for_factory(ops)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/runs/{run.id}/review/accept", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "merged"
    assert ops.lock_held_at_github_call == [True]
    assert not lock.locked()  # and released again on the way out


async def test_accept_kick_waits_for_a_held_merging_lock_before_touching_github(
    db_session, token_file, auth_headers
) -> None:
    """The other half of the same property: with the shared lock already
    held — standing in for a poller sweep mid-`_advance_all_merging` — the
    accept kick must not reach GitHub at all until it is released.

    The `pytest.raises(TimeoutError)` is the load-bearing assertion: it
    fails if the request reaches `ops.get_pr` while the lock is held, which
    is exactly what an unlocked kick does (there it arrives in milliseconds,
    far inside the 1 s window). The request still answers 200/`merged` once
    the lock is free, so the fix costs the accept path nothing but the
    wait."""
    project = await seed_project(db_session, owner="acme", repo="widgets")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="awaiting_review", pr_number=7)

    pr = PullRequest(
        number=7,
        state="open",
        merged=False,
        head_ref=f"werft/run-{run.id}",
        head_sha="head-sha-1",
        base_ref=project.unattended_branch,
        mergeable=True,
        mergeable_state="clean",
        html_url="https://github.com/acme/widgets/pull/7",
    )
    reached_github = asyncio.Event()

    class SignallingOps(FakeMergeOps):
        async def get_pr(self, number: int) -> PullRequest:
            reached_github.set()
            return await super().get_pr(number)

    ops = SignallingOps(pr)
    app = make_client_app(db_session, token_file=token_file, ops_for=ops_for_factory(ops))
    lock = app.state.merging_lock

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await lock.acquire()
        task = asyncio.create_task(
            client.post(f"/api/v1/runs/{run.id}/review/accept", headers=auth_headers)
        )
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(reached_github.wait(), timeout=1.0)
            assert ops.squash_merge_calls == []
        except BaseException:
            task.cancel()  # never leave the request in flight past a failure
            raise
        finally:
            lock.release()

        resp = await asyncio.wait_for(task, timeout=10)

    assert resp.status_code == 200
    assert resp.json()["status"] == "merged"
    assert ops.squash_merge_calls == [(7, "head-sha-1", f"werft: {project.slug} run {run.id}")]


async def test_accept_kick_skips_the_decision_table_when_a_sweep_already_merged_the_run(
    db_session, migrated_db, token_file, auth_headers
) -> None:
    """Winning the lock *second* must be a no-op, not a second opinion.

    `advance_merging` has no status guard of its own — the poller's guard is
    its `WHERE status = 'merging'` discovery query, taken inside the lock —
    so a kick that re-reads a row some sweep already advanced would re-drive
    the whole decision table on it. Here the sweep landed the merge while
    holding the lock (`merging -> merged`, real `merge_commit_sha`), so the
    PR now reads `merged=True`: unguarded, the kick takes the
    merged-out-of-band branch and CASes `merged -> merged` with
    `merge_commit_sha=None`. That CAS *wins* — the transition trigger only
    checks legality when the status actually changes (`NEW.status IS
    DISTINCT FROM OLD.status`), so nothing rejects it — and a legitimately
    merged run silently loses its merge commit sha, with no event row to
    show for it.

    The rival advance runs on its own engine, committed, exactly as the
    orchestrator's own session would — and the row is left untouched by the
    kick down to its `version`."""
    project = await seed_project(db_session, owner="acme", repo="widgets")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="awaiting_review", pr_number=7)

    # The PR as GitHub reports it *after* the sweep's merge landed.
    pr = PullRequest(
        number=7,
        state="closed",
        merged=True,
        head_ref=f"werft/run-{run.id}",
        head_sha="head-sha-1",
        base_ref=project.unattended_branch,
        mergeable=None,
        mergeable_state="unknown",
        html_url="https://github.com/acme/widgets/pull/7",
    )
    ops = FakeMergeOps(pr)
    app = make_client_app(db_session, token_file=token_file, ops_for=ops_for_factory(ops))
    lock = app.state.merging_lock
    rival_engine = create_async_engine(migrated_db)

    async def run_status() -> str:
        async with rival_engine.connect() as conn:
            return (
                await conn.execute(text("SELECT status FROM runs WHERE id = :id"), {"id": run.id})
            ).scalar_one()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await lock.acquire()
            task = asyncio.create_task(
                client.post(f"/api/v1/runs/{run.id}/review/accept", headers=auth_headers)
            )
            try:
                # Wait for the accept CAS to commit (`merging` is the only
                # state a sweep could have picked this row up from), then
                # land the sweep's merge while the kick is still parked on
                # the lock.
                for _ in range(200):
                    if await run_status() == "merging":
                        break
                    await asyncio.sleep(0.01)
                else:  # pragma: no cover - the CAS commits well inside 2 s
                    raise AssertionError("the accept CAS never committed")

                async with rival_engine.begin() as conn:
                    await conn.execute(
                        text(
                            "UPDATE runs SET status = 'merged', version = version + 1, "
                            "merge_commit_sha = 'sweep-sha-9' WHERE id = :id"
                        ),
                        {"id": run.id},
                    )
            except BaseException:
                task.cancel()
                raise
            finally:
                lock.release()

            resp = await asyncio.wait_for(task, timeout=10)
    finally:
        await rival_engine.dispose()

    assert resp.status_code == 200
    assert resp.json()["status"] == "merged"

    fresh = await db_session.get(Run, run.id, populate_existing=True)
    assert fresh.status == "merged"
    assert fresh.merge_commit_sha == "sweep-sha-9"  # the sweep's, not NULLed
    assert fresh.version == 2  # accept's CAS, then the sweep's — nothing else
    # The decision table never ran a second time.
    assert ops.squash_merge_calls == []
    assert ops.remove_label_calls == []
    assert ops.delete_ref_calls == []


async def test_accept_404_for_unknown_run(db_session, token_file, auth_headers) -> None:
    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/runs/{uuid.uuid4()}/review/accept", headers=auth_headers)
    assert resp.status_code == 404


# -- review reject ------------------------------------------------------------


async def test_reject_parks_with_reason_and_fires_alert(
    db_session, token_file, auth_headers
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="awaiting_review")
    spy = SpyAlertSink()

    app = make_client_app(db_session, token_file=token_file, alerts=spy)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/runs/{run.id}/review/reject", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "parked"
    assert body["parked_reason"] == "review_rejected"

    assert spy.run_parked_calls == [(project.slug, run.id, "review_rejected")]

    fresh = await db_session.get(Run, run.id, populate_existing=True)
    assert fresh.status == "parked"
    assert fresh.parked_reason == "review_rejected"


async def test_reject_on_running_run_is_409(db_session, token_file, auth_headers) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="running")

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/runs/{run.id}/review/reject", headers=auth_headers)

    assert resp.status_code == 409


# -- cancel -------------------------------------------------------------------


async def test_cancel_from_awaiting_review_succeeds(db_session, token_file, auth_headers) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="awaiting_review")

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/runs/{run.id}/cancel", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "canceled"


async def test_cancel_on_merged_run_is_409(db_session, token_file, auth_headers) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="merged")

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/runs/{run.id}/cancel", headers=auth_headers)

    assert resp.status_code == 409
    fresh = await db_session.get(Run, run.id, populate_existing=True)
    assert fresh.status == "merged"


# -- cancel: quota true-up (Task 17) ---------------------------------------
#
# SPEC §7: "A reservation is also released ... in the same transaction as
# any transition out of `claimed`/`running` that does not pass through CLI
# exit (lease expiry, hard-deadline sweep, cancel): no path leaks headroom."
# Cancel is the path an operator drives by hand, and it was the one leaking.


async def seed_provider_account(session: AsyncSession, *, label: str) -> uuid.UUID:
    account_id = (
        await session.execute(
            text(
                "INSERT INTO provider_accounts (provider, label, rolling_window_hours,"
                " ceiling_seconds) VALUES ('claude', :l, 5, 18000) RETURNING id"
            ),
            {"l": label},
        )
    ).scalar_one()
    await session.commit()
    return account_id


async def seed_open_reservation(
    session: AsyncSession, run: Run, *, account_id: uuid.UUID, attempt_no: int, reserved: int
) -> int:
    ledger_id = (
        await session.execute(
            text(
                "INSERT INTO quota_ledger (provider_account_id, run_id, attempt_no,"
                " reserved_wallclock_s) VALUES (:a, :r, :n, :res) RETURNING id"
            ),
            {"a": account_id, "r": run.id, "n": attempt_no, "res": reserved},
        )
    ).scalar_one()
    await session.commit()
    return ledger_id


async def seed_closed_reservation(
    session: AsyncSession, run: Run, *, account_id: uuid.UUID, attempt_no: int, actual: int
) -> int:
    ledger_id = (
        await session.execute(
            text(
                "INSERT INTO quota_ledger (provider_account_id, run_id, attempt_no,"
                " reserved_wallclock_s, actual_wallclock_s) VALUES (:a, :r, :n, :res, :act)"
                " RETURNING id"
            ),
            {"a": account_id, "r": run.id, "n": attempt_no, "res": actual or 1, "act": actual},
        )
    ).scalar_one()
    await session.commit()
    return ledger_id


async def seed_open_attempt(
    session: AsyncSession,
    run: Run,
    *,
    attempt_no: int,
    started_minutes_ago: float = 0,
    provider: str = "claude",
) -> None:
    await session.execute(
        text(
            "INSERT INTO run_attempts (run_id, attempt_no, provider, started_at)"
            " VALUES (:r, :n, :p, now() - make_interval(secs => CAST(:s AS double precision)))"
        ),
        {"r": run.id, "n": attempt_no, "p": provider, "s": started_minutes_ago * 60},
    )
    await session.commit()


async def set_container_id(session: AsyncSession, run: Run, container_id: str | None) -> None:
    await session.execute(
        text("UPDATE runs SET container_id = :c WHERE id = :r"), {"c": container_id, "r": run.id}
    )
    await session.commit()
    # The raw `UPDATE` above bypasses the ORM, so `run` (and any other
    # identity-mapped `Run` instance for this id in this session, including
    # the one `cancel_run`'s own `session.get(Run, run_id)` would otherwise
    # hand back from cache) still carries the pre-update `container_id`
    # unless refreshed from the database.
    await session.refresh(run)


async def test_cancelling_a_running_run_returns_its_reserved_headroom(
    db_session, token_file, auth_headers
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="running")
    await set_container_id(db_session, run, "c1")
    account_id = await seed_provider_account(db_session, label=f"acct-{run.id.hex}")
    ledger_id = await seed_open_reservation(
        db_session, run, account_id=account_id, attempt_no=1, reserved=5400
    )
    await seed_open_attempt(db_session, run, attempt_no=1, started_minutes_ago=5)

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/runs/{run.id}/cancel", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "canceled"

    entry = await db_session.get(QuotaLedgerEntry, ledger_id, populate_existing=True)
    assert entry.actual_wallclock_s is not None
    assert 0 < entry.actual_wallclock_s <= 5400

    attempt = (
        await db_session.execute(
            select(RunAttempt).where(RunAttempt.run_id == run.id, RunAttempt.attempt_no == 1)
        )
    ).scalar_one()
    assert attempt.outcome == "canceled"
    assert attempt.ended_at is not None


async def test_cancelling_a_claimed_run_releases_the_whole_reservation(
    db_session, token_file, auth_headers
) -> None:
    """Nothing ever started; the window owes nothing."""
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="claimed")
    account_id = await seed_provider_account(db_session, label=f"acct-{run.id.hex}")
    ledger_id = await seed_open_reservation(
        db_session, run, account_id=account_id, attempt_no=1, reserved=5400
    )
    await seed_open_attempt(db_session, run, attempt_no=1)

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/runs/{run.id}/cancel", headers=auth_headers)

    assert resp.status_code == 200
    entry = await db_session.get(QuotaLedgerEntry, ledger_id, populate_existing=True)
    assert entry.actual_wallclock_s == 0


async def test_cancelling_a_queued_run_touches_no_ledger_and_no_attempt(
    db_session, token_file, auth_headers
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="queued")

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/runs/{run.id}/cancel", headers=auth_headers)

    assert resp.status_code == 200
    ledger_rows = (
        await db_session.execute(select(QuotaLedgerEntry).where(QuotaLedgerEntry.run_id == run.id))
    ).all()
    assert ledger_rows == []
    attempt_rows = (
        await db_session.execute(select(RunAttempt).where(RunAttempt.run_id == run.id))
    ).all()
    assert attempt_rows == []


async def test_cancel_after_finalize_does_not_re_close_the_reservation(
    db_session, token_file, auth_headers
) -> None:
    """Decision 17, the other direction: the driver already released, and the
    first-writer-wins guard must leave its number alone."""
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="awaiting_review")
    account_id = await seed_provider_account(db_session, label=f"acct-{run.id.hex}")
    ledger_id = await seed_closed_reservation(
        db_session, run, account_id=account_id, attempt_no=1, actual=300
    )

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/runs/{run.id}/cancel", headers=auth_headers)

    assert resp.status_code == 200
    entry = await db_session.get(QuotaLedgerEntry, ledger_id, populate_existing=True)
    assert entry.actual_wallclock_s == 300


async def test_cancel_never_calls_docker(db_session, token_file, auth_headers, monkeypatch) -> None:
    """D10: teardown ownership is unambiguous. An API handler that reached for
    the socket would be a second owner of containers. `cancel_run` never
    imports `DockerClient` at all; constructing one here is the load-bearing
    proof that the route's true-up path never does either."""
    from werft.runner.docker_api import DockerClient

    def _unexpected_construction(*args, **kwargs):
        raise AssertionError("cancel_run must never construct a DockerClient")

    monkeypatch.setattr(DockerClient, "__init__", _unexpected_construction)

    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="running")
    await set_container_id(db_session, run, "c1")
    account_id = await seed_provider_account(db_session, label=f"acct-{run.id.hex}")
    await seed_open_reservation(db_session, run, account_id=account_id, attempt_no=1, reserved=5400)
    await seed_open_attempt(db_session, run, attempt_no=1)

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/runs/{run.id}/cancel", headers=auth_headers)

    assert resp.status_code == 200


# -- requeue --------------------------------------------------------------


async def test_requeue_resets_attempt_count_next_attempt_at_and_clears_parked_reason(
    db_session, token_file, auth_headers
) -> None:
    """Requeue is the only exit from `parked` back into the working states,
    and no other writer ever nulls `parked_reason` — every one of them sets
    it, at a park. Left behind, the reason is reported by `/runs` and
    `/runs/{id}` (and rendered by the dashboard's "Parked reason" column)
    for a run that is queued, running, or even merged. Asserted in the
    requeue response itself *and* in a follow-up read, since the response is
    a re-read of the row rather than an echo of the request."""
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(
        db_session,
        project,
        item,
        status="parked",
        attempt_count=3,
        next_attempt_at_offset_seconds=3600,
        parked_reason="ci_red",
    )
    stale_next_attempt_at = run.next_attempt_at

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/runs/{run.id}/requeue", headers=auth_headers)
        detail = await client.get(f"/api/v1/runs/{run.id}", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["attempt_count"] == 0
    assert body["parked_reason"] is None

    assert detail.status_code == 200
    assert detail.json()["parked_reason"] is None

    fresh = await db_session.get(Run, run.id, populate_existing=True)
    assert fresh.attempt_count == 0
    assert fresh.next_attempt_at > stale_next_attempt_at
    assert fresh.parked_reason is None


async def test_requeue_only_works_from_parked(db_session, token_file, auth_headers) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="queued")

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/runs/{run.id}/requeue", headers=auth_headers)

    assert resp.status_code == 409
    fresh = await db_session.get(Run, run.id, populate_existing=True)
    assert fresh.status == "queued"
    assert fresh.attempt_count == 0


# -- project onboard ----------------------------------------------------------


async def test_onboard_happy_path_returns_201_and_project(
    db_session, token_file, auth_headers
) -> None:
    ops = FakeRepoOps()
    admin_ops = FakeAdminOps()
    app = make_client_app(
        db_session,
        token_file=token_file,
        ops_for=ops_for_factory(ops),
        admin_ops_for=admin_ops_for_factory(admin_ops),
    )
    tag = uuid.uuid4().hex[:8]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/projects/onboard",
            json={"slug": f"proj-{tag}", "owner": "acme", "repo": f"widgets-{tag}"},
            headers=auth_headers,
        )

    assert resp.status_code == 201
    body = resp.json()
    assert set(body.keys()) == {
        "id",
        "slug",
        "owner",
        "repo",
        "lifecycle",
        "onboarded_at",
        "created_at",
    }
    assert body["slug"] == f"proj-{tag}"
    assert body["owner"] == "acme"
    assert body["repo"] == f"widgets-{tag}"
    assert body["lifecycle"] == "bootstrap"
    assert body["onboarded_at"] is not None

    assert admin_ops.partial_calls == ["unattended"]
    assert admin_ops.strict_calls == []
    assert ops.ensure_branch_calls == [("unattended", "main-sha-abc123")]


async def test_onboard_duplicate_slug_is_409(db_session, token_file, auth_headers) -> None:
    ops = FakeRepoOps()
    admin_ops = FakeAdminOps()
    app = make_client_app(
        db_session,
        token_file=token_file,
        ops_for=ops_for_factory(ops),
        admin_ops_for=admin_ops_for_factory(admin_ops),
    )
    tag = uuid.uuid4().hex[:8]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/projects/onboard",
            json={"slug": f"dup-{tag}", "owner": "acme", "repo": f"one-{tag}"},
            headers=auth_headers,
        )
        assert first.status_code == 201

        second = await client.post(
            "/api/v1/projects/onboard",
            json={"slug": f"dup-{tag}", "owner": "acme", "repo": f"two-{tag}"},
            headers=auth_headers,
        )

    assert second.status_code == 409


async def test_onboard_rejects_an_invalid_slug_as_422_not_409(
    db_session, token_file, auth_headers
) -> None:
    """A CHECK violation (`projects_slug_check`) is a bad request, not a
    duplicate. The old mapping answered 409 'already onboarded' for every
    `IntegrityError` at all — telling the operator to go look for a project
    that was never created, and telling a retry loop to give up."""
    ops = FakeRepoOps()
    admin_ops = FakeAdminOps()
    app = make_client_app(
        db_session,
        token_file=token_file,
        ops_for=ops_for_factory(ops),
        admin_ops_for=admin_ops_for_factory(admin_ops),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/projects/onboard",
            json={"slug": "Not A Slug", "owner": "ken", "repo": "elastic"},
            headers=auth_headers,
        )

    assert resp.status_code == 422
    assert "already onboarded" not in resp.json()["detail"]


async def test_onboard_duplicate_slug_is_409_without_message_sniffing(
    db_session, token_file, auth_headers
) -> None:
    """And the 409-vs-422 split must not depend on the words in a message:
    `duplicate_project_message`'s wording was load-bearing for HTTP
    semantics, one copy-edit away from turning every duplicate into a 422."""
    ops = FakeRepoOps()
    admin_ops = FakeAdminOps()
    app = make_client_app(
        db_session,
        token_file=token_file,
        ops_for=ops_for_factory(ops),
        admin_ops_for=admin_ops_for_factory(admin_ops),
    )
    tag = uuid.uuid4().hex[:8]
    body = {"slug": f"elastic-{tag}", "owner": "ken", "repo": f"elastic-{tag}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/v1/projects/onboard", json=body, headers=auth_headers)
        assert first.status_code == 201

        second = await client.post("/api/v1/projects/onboard", json=body, headers=auth_headers)

    assert second.status_code == 409


async def test_onboard_concurrent_duplicate_is_409_not_500(
    db_session, migrated_db, token_file, auth_headers
) -> None:
    """`onboard_project`'s duplicate `SELECT` runs before four awaited
    GitHub round trips, so a second onboard of the same slug/repo can commit
    its own row inside that window: both requests pass the check, and the
    loser's INSERT violates `projects`' unique constraints. Uncaught, that
    `IntegrityError` is a 500 on a path whose own docstring promises 409.

    The race is simulated deterministically rather than by timing — the
    rival row is inserted, and committed, from a second engine at the last
    GitHub call this request makes, i.e. squarely between the request's own
    check and its INSERT."""
    tag = uuid.uuid4().hex[:8]
    slug, owner, repo = f"race-{tag}", "acme", f"widgets-{tag}"
    rival_engine = create_async_engine(migrated_db)

    class RacingOps(FakeRepoOps):
        async def ensure_label(self, name: str, color: str) -> None:
            await super().ensure_label(name, color)
            async with rival_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO projects (slug, github_owner, github_repo) "
                        "VALUES (:slug, :owner, :repo)"
                    ),
                    {"slug": slug, "owner": owner, "repo": repo},
                )

    ops = RacingOps()
    admin_ops = FakeAdminOps()
    app = make_client_app(
        db_session,
        token_file=token_file,
        ops_for=ops_for_factory(ops),
        admin_ops_for=admin_ops_for_factory(admin_ops),
    )
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/projects/onboard",
                json={"slug": slug, "owner": owner, "repo": repo},
                headers=auth_headers,
            )
    finally:
        await rival_engine.dispose()

    assert resp.status_code == 409
    # Same body as the sequential duplicate — one race, one answer.
    assert resp.json() == {"detail": duplicate_project_message(slug, owner, repo)}


async def test_onboard_unreachable_installation_is_422(
    db_session, token_file, auth_headers
) -> None:
    ops = FakeRepoOps(main_sha=None)
    admin_ops = FakeAdminOps()
    app = make_client_app(
        db_session,
        token_file=token_file,
        ops_for=ops_for_factory(ops),
        admin_ops_for=admin_ops_for_factory(admin_ops),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/projects/onboard",
            json={"slug": "no-main", "owner": "acme", "repo": "ghost"},
            headers=auth_headers,
        )

    assert resp.status_code == 422
    assert admin_ops.partial_calls == []


async def test_onboard_503_when_github_unconfigured(db_session, token_file, auth_headers) -> None:
    app = make_client_app(db_session, token_file=token_file)  # ops_for/admin_ops_for stay None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/projects/onboard",
            json={"slug": "x", "owner": "acme", "repo": "x"},
            headers=auth_headers,
        )

    assert resp.status_code == 503


# -- manual flip ----------------------------------------------------------


async def test_flip_bootstrap_to_oracle_gated_applies_strict_protection(
    db_session, token_file, auth_headers
) -> None:
    project = await seed_project(db_session)  # lifecycle defaults to bootstrap
    admin_ops = FakeAdminOps()
    spy = SpyAlertSink()
    app = make_client_app(
        db_session,
        token_file=token_file,
        admin_ops_for=admin_ops_for_factory(admin_ops),
        alerts=spy,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/projects/{project.id}/flip",
            json={"to": "oracle_gated"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["lifecycle"] == "oracle_gated"

    assert admin_ops.strict_calls == [project.unattended_branch]
    assert admin_ops.partial_calls == []
    assert spy.project_flipped_calls == [project.slug]


async def test_flip_oracle_gated_to_bootstrap_applies_partial_protection(
    db_session, token_file, auth_headers
) -> None:
    project = await seed_project(db_session)
    await db_session.execute(
        text("UPDATE projects SET lifecycle = 'oracle_gated' WHERE id = :id"), {"id": project.id}
    )
    await db_session.commit()

    admin_ops = FakeAdminOps()
    spy = SpyAlertSink()
    app = make_client_app(
        db_session,
        token_file=token_file,
        admin_ops_for=admin_ops_for_factory(admin_ops),
        alerts=spy,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/projects/{project.id}/flip",
            json={"to": "bootstrap"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert resp.json()["lifecycle"] == "bootstrap"

    assert admin_ops.partial_calls == [project.unattended_branch]
    assert admin_ops.strict_calls == []
    # Downgrade is manual repair, not the doctrine-#1 milestone — never alerts.
    assert spy.project_flipped_calls == []


async def test_flip_409_when_already_in_target_state(db_session, token_file, auth_headers) -> None:
    project = await seed_project(db_session)  # already bootstrap
    admin_ops = FakeAdminOps()
    app = make_client_app(
        db_session, token_file=token_file, admin_ops_for=admin_ops_for_factory(admin_ops)
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/projects/{project.id}/flip",
            json={"to": "bootstrap"},
            headers=auth_headers,
        )

    assert resp.status_code == 409
    assert admin_ops.strict_calls == []
    assert admin_ops.partial_calls == []


async def test_flip_503_when_github_unconfigured(db_session, token_file, auth_headers) -> None:
    project = await seed_project(db_session)
    app = make_client_app(db_session, token_file=token_file)  # admin_ops_for stays None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/projects/{project.id}/flip",
            json={"to": "oracle_gated"},
            headers=auth_headers,
        )

    assert resp.status_code == 503


async def test_flip_404_for_unknown_project(db_session, token_file, auth_headers) -> None:
    admin_ops = FakeAdminOps()
    app = make_client_app(
        db_session, token_file=token_file, admin_ops_for=admin_ops_for_factory(admin_ops)
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/projects/{uuid.uuid4()}/flip",
            json={"to": "oracle_gated"},
            headers=auth_headers,
        )

    assert resp.status_code == 404


# -- auth wiring reaches every new endpoint too --------------------------------


async def test_new_b3_endpoints_require_auth(db_session, token_file) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="awaiting_review")

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accept_resp = await client.post(f"/api/v1/runs/{run.id}/review/accept")
        reject_resp = await client.post(f"/api/v1/runs/{run.id}/review/reject")
        cancel_resp = await client.post(f"/api/v1/runs/{run.id}/cancel")
        requeue_resp = await client.post(f"/api/v1/runs/{run.id}/requeue")
        onboard_resp = await client.post(
            "/api/v1/projects/onboard", json={"slug": "x", "owner": "a", "repo": "b"}
        )
        flip_resp = await client.post(
            f"/api/v1/projects/{project.id}/flip", json={"to": "bootstrap"}
        )

    for resp in (
        accept_resp,
        reject_resp,
        cancel_resp,
        requeue_resp,
        onboard_resp,
        flip_resp,
    ):
        assert resp.status_code == 401
