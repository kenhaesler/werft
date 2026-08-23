"""The attempt driver, end to end against a real Postgres and a real git origin
(SPEC §4.3; plan D1/D6/D7 and decisions 15-17).

Docker and GitHub are faked — those are the two surfaces this milestone cannot
exercise in CI — but nothing else is: the clone is a real `git clone` of a real
local repository, the transitions go through the real trigger-enforced state
machine, and the quota ledger is the real one.
"""

import asyncio
import json
import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.fakes import (
    FakeAuth,
    FakeDocker,
    FakeRepoOps,
    SpyAlerts,
    make_origin,
    token,
    write_outputs,
)
from werft.config.dispatch import DispatchConfigCache
from werft.config.settings import Settings
from werft.db.models import ProviderAccount, QuotaLedgerEntry, Run, RunAttempt, RunEvent
from werft.domain.runs import run_branch_name
from werft.github.client import GitHubUnavailable
from werft.observe.alerts import NullAlertSink
from werft.orchestrator.dispatch import claim_next
from werft.orchestrator.driver import DriverDeps, attend_run
from werft.orchestrator.sweeps import SweepDeps, sweep_leases
from werft.providers.claude import ClaudeSpec
from werft.quota.ledger import LedgerQuota
from werft.runner.git import GitError
from werft.runner.workspace import create_run_dirs, placement_for

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git binary not on PATH")

DIGEST = "werft-runner-elastic@sha256:" + "d" * 64
CREDENTIAL = "oauth-token-from-file"


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def origin(tmp_path) -> tuple[str, str]:
    return make_origin(tmp_path / "origin")


@pytest.fixture
async def deps_fixture(migrated_db, db_session, tmp_path, monkeypatch, origin):
    from werft.orchestrator import driver as driver_module

    remote_uri, origin_sha = origin
    tag = uuid.uuid4().hex[:8]
    slug = f"p{tag}"

    project_id = (
        await db_session.execute(
            text(
                "INSERT INTO projects (slug, github_owner, github_repo, unattended_branch)"
                " VALUES (:s, 'ken', :r, 'unattended') RETURNING id"
            ),
            {"s": slug, "r": f"repo{tag}"},
        )
    ).scalar_one()
    item_id = (
        await db_session.execute(
            text(
                "INSERT INTO backlog_items (project_id, github_issue_number, title, body,"
                " github_updated_at) VALUES (:p, 7, 'make it work', 'the body', now())"
                " RETURNING id"
            ),
            {"p": project_id},
        )
    ).scalar_one()
    await db_session.execute(
        text(
            "INSERT INTO runs (project_id, backlog_item_id, status, next_attempt_at)"
            " VALUES (:p, :i, 'queued', now() - interval '1 minute')"
        ),
        {"p": project_id, "i": item_id},
    )
    account_label = f"a{tag}"
    await db_session.execute(
        text(
            "INSERT INTO provider_accounts (provider, label, rolling_window_hours,"
            " ceiling_seconds) VALUES ('claude', :l, 5, 18000)"
        ),
        {"l": account_label},
    )
    await db_session.commit()

    credential = tmp_path / "claude-credential"
    credential.write_text(CREDENTIAL + "\n", encoding="utf-8")
    config_file = tmp_path / "dispatch.json"
    config_file.write_text(
        json.dumps(
            {
                "projects": {
                    slug: {
                        "image_digest": DIGEST,
                        "model": "claude-sonnet-4-6",
                        "timeout_seconds": 1800,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    settings = Settings(
        runs_root=str(tmp_path / "runs"),
        claude_credential_file=str(credential),
        dispatch_config_file=str(config_file),
        lease_seconds=120,
        heartbeat_seconds=1,
        max_concurrent_runs=8,
    )
    config = DispatchConfigCache(str(config_file))
    quota = LedgerQuota(label=account_label, typical_reservation_seconds=1800)

    engine = create_async_engine(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        outcome = await claim_next(
            session,
            quota=quota,
            config=config.current(),
            settings=settings,
            alerts=NullAlertSink(),
            now=datetime.now(UTC),
            live_driver_count=0,
        )
    assert outcome.status == "claimed"
    run_id = outcome.run_id

    docker = FakeDocker()
    ops = FakeRepoOps()
    auth = FakeAuth()
    alerts = SpyAlerts()
    fakes = SimpleNamespace(
        docker=docker,
        ops=ops,
        auth=auth,
        alerts=alerts,
        origin_sha=origin_sha,
        git_remote=remote_uri,
        git_error=None,
        on_started=None,
        git=SimpleNamespace(clone_calls=[]),
        factory=factory,
        settings=settings,
        slug=slug,
        account_label=account_label,
    )
    docker.fakes = fakes
    docker.placement = placement_for(
        run_id, runs_root=settings.runs_root, dns_ip=settings.runner_dns_ip
    )

    monkeypatch.setattr(driver_module, "remote_url", lambda **_kwargs: fakes.git_remote)

    real_clone = driver_module.clone_workspace

    async def recording_clone(**kwargs):
        fakes.git.clone_calls.append(kwargs)
        if fakes.git_error is not None:
            raise fakes.git_error
        return await real_clone(**kwargs)

    monkeypatch.setattr(driver_module, "clone_workspace", recording_clone)

    deps = DriverDeps(
        session_factory=factory,
        docker=docker,
        auth=auth,
        ops_for=lambda _project: ops,
        alerts=alerts,
        quota=quota,
        spec=ClaudeSpec(),
        settings=settings,
        config=config,
    )
    try:
        yield deps, run_id, fakes
    finally:
        await engine.dispose()


# --- readers -----------------------------------------------------------------


async def await_running(fakes, run_id, *, timeout: float = 10.0) -> Run:
    """`FakeDocker.started` fires inside `start_container`, which is a couple of
    awaits before the `claimed -> running` CAS commits. Every assertion about
    the running row has to wait for the CAS, not for the start."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        row = await fetch(fakes, run_id)
        if row.status == "running":
            return row
        assert asyncio.get_running_loop().time() < deadline, f"still {row.status}"
        await asyncio.sleep(0.02)


async def fetch(fakes, run_id) -> Run:
    async with fakes.factory() as session:
        return await session.get(Run, run_id)


async def latest_attempt(fakes, run_id) -> RunAttempt:
    async with fakes.factory() as session:
        return (
            await session.execute(
                select(RunAttempt)
                .where(RunAttempt.run_id == run_id)
                .order_by(RunAttempt.attempt_no.desc())
                .limit(1)
            )
        ).scalar_one()


async def ledger_entry(fakes, run_id) -> QuotaLedgerEntry:
    async with fakes.factory() as session:
        return (
            await session.execute(
                select(QuotaLedgerEntry)
                .where(QuotaLedgerEntry.run_id == run_id)
                .order_by(QuotaLedgerEntry.attempt_no.desc())
                .limit(1)
            )
        ).scalar_one()


async def dispatch_phases(fakes, run_id) -> list[str]:
    async with fakes.factory() as session:
        rows = (
            (
                await session.execute(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.event_type == "dispatch")
                    .order_by(RunEvent.id)
                )
            )
            .scalars()
            .all()
        )
    return [row.payload["phase"] for row in rows]


async def fetch_account(fakes) -> ProviderAccount:
    async with fakes.factory() as session:
        return (
            await session.execute(
                select(ProviderAccount).where(ProviderAccount.label == fakes.account_label)
            )
        ).scalar_one()


def placement_of(fakes, run_id):
    return placement_for(
        run_id, runs_root=fakes.settings.runs_root, dns_ip=fakes.settings.runner_dns_ip
    )


def run_dir_of(fakes, run_id) -> Path:
    return Path(placement_of(fakes, run_id).run_dir)


def workspace_of(fakes, run_id) -> Path:
    return Path(placement_of(fakes, run_id).workspace_dir)


def outputs_of(fakes, run_id) -> Path:
    return Path(placement_of(fakes, run_id).outputs_dir)


def secrets_of(fakes, run_id) -> Path:
    return Path(placement_of(fakes, run_id).secrets_dir)


def sweep_deps_of(driver_deps, fakes) -> SweepDeps:
    """The crash-recovery half of the same process, wired to the same database
    and the same fake daemon — so a test can ask what `tick_once`'s lease sweep
    would do to a row a driver has just left behind."""
    return SweepDeps(
        session_factory=fakes.factory,
        docker=fakes.docker,
        quota=driver_deps.quota,
        alerts=fakes.alerts,
        settings=fakes.settings,
    )


def network_of(run_id) -> str:
    return f"werft-net-{run_id}"


async def set_running_with_container(
    fakes, run_id, container_id: str, *, hard_deadline_at: datetime | None = None
) -> None:
    """The row a manager that died mid-attempt leaves behind. `hard_deadline_at`
    is left as the claim wrote it unless a test wants to move it."""
    create_run_dirs(placement_of(fakes, run_id))
    async with fakes.factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE runs SET status = 'running', version = version + 1, container_id = :c,"
                " base_sha = :s, lease_expires_at = now() + interval '2 minutes'"
                " WHERE id = :r"
            ),
            {"c": container_id, "s": fakes.origin_sha, "r": run_id},
        )
        if hard_deadline_at is not None:
            await session.execute(
                text("UPDATE runs SET hard_deadline_at = :d WHERE id = :r"),
                {"d": hard_deadline_at, "r": run_id},
            )


def write_stale_task_json(fakes, run_id, **env: str) -> Path:
    """The `task.json` a driver that died with the manager left behind. The
    re-adopting driver never writes one of its own, so this file is the only
    place the previous attempt's credentials still live."""
    path = run_dir_of(fakes, run_id) / "task.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": str(run_id), "argv": ["claude", "-p"], "env": env}, indent=2),
        encoding="utf-8",
    )
    return path


async def cancel_run_in_db(fakes, run_id, *, observed: int) -> None:
    """What the operator cancel route does (T6): close the attempt, true the
    reservation up, CAS to `canceled` — all before this driver notices."""
    async with fakes.factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE run_attempts SET ended_at = now(), duration_seconds = :d,"
                " outcome = 'canceled' WHERE run_id = :r AND ended_at IS NULL"
            ),
            {"d": observed, "r": run_id},
        )
        await session.execute(
            text(
                "UPDATE quota_ledger SET actual_wallclock_s = :d"
                " WHERE run_id = :r AND actual_wallclock_s IS NULL"
            ),
            {"d": observed, "r": run_id},
        )
        await session.execute(
            text("UPDATE runs SET status = 'canceled', version = version + 1 WHERE id = :r"),
            {"r": run_id},
        )


# --- prepare + launch --------------------------------------------------------


async def test_a_claimed_run_is_cloned_branch_reset_launched_and_moved_to_running(deps_fixture):
    driver_deps, run_id, fakes = deps_fixture
    fakes.docker.hold_die = True  # stay in `running`

    task = asyncio.create_task(attend_run(driver_deps, run_id))
    await fakes.docker.started.wait()

    row = await await_running(fakes, run_id)
    assert row.container_id == fakes.docker.container_id
    assert row.base_sha == fakes.origin_sha
    assert fakes.ops.ensure_branch_calls == [(run_branch_name(run_id), fakes.origin_sha)]
    assert fakes.ops.force_reset_calls == [(run_branch_name(run_id), fakes.origin_sha)]
    assert fakes.docker.created_bodies[0]["Labels"] == {"werft.run_id": str(run_id)}
    assert (workspace_of(fakes, run_id) / "README.md").exists()
    payload = json.loads((run_dir_of(fakes, run_id) / "task.json").read_text(encoding="utf-8"))
    assert payload["argv"][0] == "claude"
    assert "/run/secrets/prompt.md" in " ".join(payload["argv"])
    assert payload["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == CREDENTIAL
    assert sorted(os.listdir(secrets_of(fakes, run_id))) == [
        "askpass.sh",
        "git_token",
        "prompt.md",
        "system_prompt.md",
    ]
    phases = await dispatch_phases(fakes, run_id)
    assert phases[-2:] == ["workspace_ready", "container_started"]

    fakes.docker.release_die(0)
    await task


async def test_re_preparing_a_claimed_run_rebuilds_the_tree(deps_fixture):
    """A re-adopted `claimed` run that inherited the previous attempt's
    `result.json` would be classified from a stale file."""
    driver_deps, run_id, fakes = deps_fixture
    outputs = outputs_of(fakes, run_id)
    outputs.mkdir(parents=True, exist_ok=True)
    stale = outputs / "result.json"
    stale.write_text('{"status":"success"}', encoding="utf-8")

    await attend_run(driver_deps, run_id)  # `on_started` writes nothing this time

    assert not stale.exists()


# --- finalization ------------------------------------------------------------


async def test_a_pushed_success_finalizes_to_awaiting_review_and_trues_up(deps_fixture):
    """Decision 15: `pushed` is decided manager-side. The adapter hard-codes
    `result.json.pushed = False` and never inspects git, so the only honest
    signal is the run branch's head moving off `base_sha`."""
    driver_deps, run_id, fakes = deps_fixture
    fakes.ops.ref_shas[run_branch_name(run_id)] = "f" * 40
    fakes.on_started = lambda placement: write_outputs(placement, status="success")

    await attend_run(driver_deps, run_id)

    row = await fetch(fakes, run_id)
    assert row.status == "awaiting_review"  # bootstrap project
    assert row.pr_number == fakes.ops.opened_pr.number
    assert row.exit_code == 0
    assert row.result["usage"]["input_tokens"] == 11  # display-only (SPEC §7)
    assert (await ledger_entry(fakes, run_id)).actual_wallclock_s is not None
    assert fakes.auth.revoked  # SPEC §4.4
    assert f"remove_network:{network_of(run_id)}" in fakes.docker.calls
    assert (await dispatch_phases(fakes, run_id))[-1] == "container_died"


async def test_a_success_that_pushed_nothing_is_not_a_success(deps_fixture):
    driver_deps, run_id, fakes = deps_fixture
    fakes.ops.ref_shas.clear()
    fakes.on_started = lambda placement: write_outputs(placement, status="success")

    await attend_run(driver_deps, run_id)

    row = await fetch(fakes, run_id)
    assert row.status in ("queued", "parked")
    assert "without push" in (row.error_message or "")
    assert not fakes.ops.open_pr_calls


async def test_github_unavailable_during_push_detection_defers_instead_of_failing(deps_fixture):
    """Decision 15: leave the run `running` and let the lease and the next
    attend re-drive, rather than declaring "not pushed" and throwing the work
    away."""
    driver_deps, run_id, fakes = deps_fixture
    fakes.ops.ref_error = GitHubUnavailable(503, "down")
    fakes.on_started = lambda placement: write_outputs(placement, status="success")

    await attend_run(driver_deps, run_id)

    assert (await fetch(fakes, run_id)).status == "running"
    assert (await ledger_entry(fakes, run_id)).actual_wallclock_s is None  # nothing settled


async def test_github_unavailable_at_finalize_leaves_the_whole_attempt_standing(deps_fixture):
    """Decision 15 says "not a verdict, re-drive" — so the thing to re-drive has
    to still exist. Returning early from `_finalize` used to fall through to
    `run()`'s teardown, which removed the container and network and revoked the
    git token while the row still said `running`: the re-adopting driver then
    had nothing but a die event that may have aged out of the daemon's buffer.
    """
    driver_deps, run_id, fakes = deps_fixture
    fakes.ops.ref_error = GitHubUnavailable(503, "down")
    fakes.on_started = lambda placement: write_outputs(placement, status="success")

    await attend_run(driver_deps, run_id)

    assert (await fetch(fakes, run_id)).status == "running"
    assert not any(c.startswith("remove_container") for c in fakes.docker.calls)
    assert f"remove_network:{network_of(run_id)}" not in fakes.docker.calls
    assert not fakes.auth.revoked  # the git token the box still reads on every push
    assert (secrets_of(fakes, run_id) / "git_token").exists()

    # And the re-drive lands it, off the container this driver refused to remove.
    fakes.ops.ref_error = None
    fakes.ops.ref_shas[run_branch_name(run_id)] = "f" * 40

    await attend_run(driver_deps, run_id)

    assert (await fetch(fakes, run_id)).status == "awaiting_review"
    assert len(fakes.git.clone_calls) == 1  # re-adopted, not re-prepared
    assert f"remove_container:{fakes.docker.container_id}" in fakes.docker.calls
    assert fakes.auth.revoked


async def test_a_deferred_finalize_leaves_a_lease_that_can_still_expire(deps_fixture):
    """The other half of the invariant, pinned so the renewal below cannot be
    mistaken for "the deferred row is immortal".

    A defer renews the lease once and then this driver is gone: the ticker was
    cancelled when `_attend` returned and `run()` completing pops the run out of
    `Orchestrator._drivers`. If no re-drive ever comes — the manager died — the
    lease runs out and the sweep is right to claim the row.
    """
    driver_deps, run_id, fakes = deps_fixture
    fakes.ops.ref_error = GitHubUnavailable(503, "down")
    fakes.on_started = lambda placement: write_outputs(placement, status="success")

    await attend_run(driver_deps, run_id)

    deferred = await fetch(fakes, run_id)
    assert deferred.status == "running"

    # No live driver (this one has returned), and the synthetic clock is past
    # the lease the defer wrote.
    after = deferred.lease_expires_at + timedelta(seconds=1)
    assert await sweep_leases(sweep_deps_of(driver_deps, fakes), now=after, live=set()) == 1


async def test_a_re_drive_after_a_deferred_finalize_keeps_the_lease_honest(deps_fixture):
    """The finding: `_finalize`'s GitHub-unavailable defer used to return with
    the lease frozen at container death, while the heartbeat ticker was already
    cancelled and `run()` completing dropped the run from the live-driver
    registry. `sweep_leases` — which runs *before* the attend sweep in
    `tick_once` — would then see an expired lease and no registry entry roughly
    a lease later, close the attempt as an `infra_failure` and let the retry
    `force_reset_ref` the branch, discarding work the attempt may already have
    pushed.

    The invariant is "a row is swept only when it has neither a live driver NOR
    a valid lease". Each re-drive that lands back on the defer renews the lease,
    so the row becomes sweepable exactly when the re-drives stop.
    """
    driver_deps, run_id, fakes = deps_fixture
    fakes.ops.ref_error = GitHubUnavailable(503, "down")
    fakes.on_started = lambda placement: write_outputs(placement, status="success")

    await attend_run(driver_deps, run_id)
    first = (await fetch(fakes, run_id)).lease_expires_at

    # The next attend sweep re-drives it, GitHub is still down, it defers again.
    await asyncio.sleep(0.5)
    await attend_run(driver_deps, run_id)

    row = await fetch(fakes, run_id)
    assert row.status == "running"
    assert row.lease_expires_at > first  # the re-drive bought another lease

    # An instant that is past the *first* lease — so the row would have been
    # swept without the re-drive — but inside the one the re-drive wrote.
    between = first + (row.lease_expires_at - first) / 2
    assert between > first
    assert await sweep_leases(sweep_deps_of(driver_deps, fakes), now=between, live=set()) == 0
    assert (await fetch(fakes, run_id)).status == "running"
    assert (await ledger_entry(fakes, run_id)).actual_wallclock_s is None  # nothing settled


async def test_a_defer_on_a_row_somebody_else_settled_tears_down_now(deps_fixture):
    """The defer exists to protect a re-drive. When there is no re-drive left to
    protect, it protects nothing and costs a box.

    `_renew_lease`'s guard is `status IN (claimed, running)`, so a `False`
    return is proof the row has already left both — a sweep or an operator
    cancel settled this attempt while GitHub was unreachable. Skipping teardown
    there would leave the container, the network and a live git token to the
    orphan sweep minutes later; the driver falls through to its normal teardown
    instead.
    """
    driver_deps, run_id, fakes = deps_fixture
    fakes.on_started = lambda placement: write_outputs(placement, status="success")

    async def settled_by_somebody_else_then_unavailable(_branch):
        # The concurrent actor lands *between* the die event and the defer, so
        # the renewal inside `_finalize` is the first thing to see it.
        await cancel_run_in_db(fakes, run_id, observed=12)
        raise GitHubUnavailable(503, "down")

    fakes.ops.get_ref_sha = settled_by_somebody_else_then_unavailable

    await attend_run(driver_deps, run_id)

    assert (await fetch(fakes, run_id)).status == "canceled"  # not re-drivable
    assert f"remove_container:{fakes.docker.container_id}" in fakes.docker.calls
    assert f"remove_network:{network_of(run_id)}" in fakes.docker.calls
    assert fakes.auth.revoked
    assert not (secrets_of(fakes, run_id) / "git_token").exists()


async def test_a_re_adopted_run_inherits_its_deadline_rather_than_a_fresh_ceiling(deps_fixture):
    """The ceiling `RunnerLifecycle` enforces runs from the moment *this* driver
    starts attending, so a manager that restarts at minute 89 of a 90-minute run
    would hand the container a second full timeout — and `sweep_deadlines` skips
    runs a live driver holds, so nothing else would cut it short. The driver
    clamps to `hard_deadline_at` instead.

    `asyncio.wait_for` is the assertion: unclamped, this waits the config's full
    1800 seconds and times out here rather than failing on a value.
    """
    driver_deps, run_id, fakes = deps_fixture
    fakes.docker.hold_die = True  # nothing inside the box ever ends the run
    await set_running_with_container(
        fakes,
        run_id,
        fakes.docker.container_id,
        hard_deadline_at=datetime.now(UTC) - timedelta(seconds=5),
    )

    await asyncio.wait_for(attend_run(driver_deps, run_id), timeout=60)

    assert f"kill:{fakes.docker.container_id}" in fakes.docker.calls
    assert (await latest_attempt(fakes, run_id)).outcome == "timeout"


async def test_a_quota_exhausted_classification_blocks_the_account_durably(deps_fixture):
    """D11 + #24 acceptance 3."""
    driver_deps, run_id, fakes = deps_fixture
    reset = datetime.now(UTC) + timedelta(hours=2)
    fakes.on_started = lambda p: write_outputs(
        p,
        status="failure",
        envelope_result=f"Claude usage limit reached, resets {reset.isoformat()}",
    )

    await attend_run(driver_deps, run_id)

    row = await fetch(fakes, run_id)
    assert row.status == "blocked_quota"
    assert row.attempt_count == 0  # quota is budget-exempt
    assert (await latest_attempt(fakes, run_id)).outcome == "quota_exhausted"
    account = await fetch_account(fakes)
    assert account.exhausted_until == reset and account.exhausted_source == "cli"
    assert fakes.alerts.quota_exhausted_until_calls == [("claude", reset)]


async def test_a_limit_message_without_a_reset_still_blocks_and_says_so(deps_fixture):
    driver_deps, run_id, fakes = deps_fixture
    fakes.on_started = lambda p: write_outputs(
        p, status="failure", envelope_result="Claude usage limit reached"
    )

    await attend_run(driver_deps, run_id)

    account = await fetch_account(fakes)
    assert account.exhausted_source == "cli_no_reset"
    assert (
        timedelta(minutes=14) < account.exhausted_until - datetime.now(UTC) < timedelta(minutes=16)
    )


# --- failure paths -----------------------------------------------------------


async def test_a_repo_404_parks_through_two_legal_transitions(deps_fixture, tmp_path):
    """D6: `domain/runs.py::TRANSITIONS` has no `claimed -> parked` edge, so a
    permanent failure is always `claimed -> failed -> parked`."""
    driver_deps, run_id, fakes = deps_fixture
    fakes.ops.ref_shas.clear()
    fakes.git_remote = (tmp_path / "nope").as_uri()  # clone raises PermanentError

    await attend_run(driver_deps, run_id)

    row = await fetch(fakes, run_id)
    assert (row.status, row.parked_reason) == ("parked", "permanent_error")
    assert fakes.alerts.run_parked_calls
    assert (await ledger_entry(fakes, run_id)).actual_wallclock_s == 0  # never started


async def test_a_clone_failure_rides_the_retry_ladder_and_trues_up_zero(deps_fixture):
    driver_deps, run_id, fakes = deps_fixture
    fakes.git_error = GitError(["git", "clone"], 128, "fatal: unable to access")

    await attend_run(driver_deps, run_id)

    row = await fetch(fakes, run_id)
    assert row.status == "queued" and row.next_attempt_at > datetime.now(UTC)
    assert (await latest_attempt(fakes, run_id)).outcome == "infra_failure"
    assert (await ledger_entry(fakes, run_id)).actual_wallclock_s == 0


async def test_a_launch_failure_releases_the_reservation_on_the_way_out(deps_fixture):
    driver_deps, run_id, fakes = deps_fixture
    fakes.docker.raise_on = "create_container"

    await attend_run(driver_deps, run_id)  # must not raise

    assert (await fetch(fakes, run_id)).status in ("queued", "parked")
    assert (await ledger_entry(fakes, run_id)).actual_wallclock_s is not None
    assert f"remove_network:{network_of(run_id)}" in fakes.docker.calls


# --- the ticker --------------------------------------------------------------


async def test_the_heartbeat_extends_the_lease_while_attending(deps_fixture):
    driver_deps, run_id, fakes = deps_fixture
    fakes.docker.hold_die = True

    task = asyncio.create_task(attend_run(driver_deps, run_id))
    await fakes.docker.started.wait()
    earlier = await await_running(fakes, run_id)
    await asyncio.sleep(1.4)
    later = await fetch(fakes, run_id)

    assert later.lease_expires_at > earlier.lease_expires_at
    assert later.last_heartbeat_at is not None

    fakes.docker.release_die(0)
    await task


async def test_the_token_is_reminted_by_rename_while_the_run_is_long(deps_fixture):
    """SPEC §4.4: a 90-minute run outlives a one-hour token, and the in-box
    askpass reads the file fresh on every git call."""
    driver_deps, run_id, fakes = deps_fixture
    fakes.auth.tokens = [token("ghs_one", minutes=5), token("ghs_two", minutes=60)]
    fakes.docker.hold_die = True

    task = asyncio.create_task(attend_run(driver_deps, run_id))
    await fakes.docker.started.wait()
    await await_running(fakes, run_id)
    await asyncio.sleep(1.4)

    assert (secrets_of(fakes, run_id) / "git_token").read_text(encoding="utf-8") == "ghs_two"
    assert "token_reminted" in await dispatch_phases(fakes, run_id)

    fakes.docker.release_die(0)
    await task

    assert fakes.auth.revoked == ["ghs_one", "ghs_two"]  # old at re-mint, live at teardown


# --- ownership, adoption, cancellation ---------------------------------------


async def test_a_run_canceled_mid_attempt_is_not_finalized(deps_fixture):
    """D10 + decision 17: the cancel route already closed the attempt and trued
    up quota; the driver must notice and skip, not raise from
    `_current_attempt` and not double-account."""
    driver_deps, run_id, fakes = deps_fixture
    loop = asyncio.get_running_loop()
    pending: list[asyncio.Task] = []
    fakes.on_started = lambda p: pending.append(
        loop.create_task(cancel_run_in_db(fakes, run_id, observed=42))
    )
    fakes.docker.hold_die = True

    task = asyncio.create_task(attend_run(driver_deps, run_id))
    await fakes.docker.started.wait()
    await await_running(fakes, run_id)
    await asyncio.gather(*pending)
    fakes.docker.release_die(0)
    await task

    assert (await fetch(fakes, run_id)).status == "canceled"
    assert (await ledger_entry(fakes, run_id)).actual_wallclock_s == 42  # the guard held
    assert (await latest_attempt(fakes, run_id)).outcome == "canceled"  # not overwritten
    assert "abandoned" in await dispatch_phases(fakes, run_id)
    assert fakes.auth.revoked  # teardown still ran


async def test_re_adopting_a_running_run_with_a_dead_container_finalizes_it(deps_fixture):
    """Crash-window row 6: no driver task exists, the container already died,
    and a fresh `attend_run` must still land the run."""
    driver_deps, run_id, fakes = deps_fixture
    await set_running_with_container(fakes, run_id, fakes.docker.container_id)
    write_outputs(placement_of(fakes, run_id), status="success")
    fakes.ops.ref_shas[run_branch_name(run_id)] = "f" * 40
    fakes.docker.release_die(0)

    await attend_run(driver_deps, run_id)

    assert (await fetch(fakes, run_id)).status == "awaiting_review"
    assert fakes.git.clone_calls == []  # re-adoption never re-clones a running run


async def test_a_lost_launch_cas_still_removes_the_container_it_just_started(deps_fixture):
    """The `claimed -> running` CAS is the only place `runs.container_id` is
    written, so a cancel that lands between `start` and that CAS leaves the row
    with a NULL container id — nothing id-based will ever find the container
    again. This driver is the last thing that knows it exists, so it removes it
    on the way out, while finalizing nothing: the attempt row and the
    reservation were closed by whoever cancelled."""
    driver_deps, run_id, fakes = deps_fixture
    fakes.on_started = lambda _placement: cancel_run_in_db(fakes, run_id, observed=5)

    await attend_run(driver_deps, run_id)

    assert f"remove_container:{fakes.docker.container_id}" in fakes.docker.calls
    assert f"remove_network:{network_of(run_id)}" in fakes.docker.calls
    row = await fetch(fakes, run_id)
    assert (row.status, row.container_id, row.exit_code) == ("canceled", None, None)
    assert (await ledger_entry(fakes, run_id)).actual_wallclock_s == 5  # not re-settled
    assert (await latest_attempt(fakes, run_id)).outcome == "canceled"  # not overwritten
    assert "container_died" not in await dispatch_phases(fakes, run_id)
    assert fakes.auth.revoked  # teardown still ran


async def test_a_re_adopted_run_scrubs_every_credential_the_stale_task_json_holds(deps_fixture):
    """D7 on the crash-recovery path: the driver that wrote this `task.json`
    died with the manager, so the scrub cannot depend on in-memory state — and
    every marker-matching value goes, not just the first."""
    driver_deps, run_id, fakes = deps_fixture
    fakes.auth.tokens = [token("ghs_readopted", minutes=60)]
    await set_running_with_container(fakes, run_id, fakes.docker.container_id)
    path = write_stale_task_json(
        fakes,
        run_id,
        CLAUDE_CODE_OAUTH_TOKEN=CREDENTIAL,
        GIT_ASKPASS_TOKEN="ghs_stale_from_the_dead_manager",
        CI="true",
    )
    write_outputs(placement_of(fakes, run_id), status="success")
    fakes.ops.ref_shas[run_branch_name(run_id)] = "f" * 40
    fakes.docker.release_die(0)

    await attend_run(driver_deps, run_id)

    retained = path.read_text(encoding="utf-8")
    assert CREDENTIAL not in retained
    assert "ghs_stale_from_the_dead_manager" not in retained
    assert "ghs_readopted" not in retained  # this driver's own git token either
    assert retained.count("<redacted>") == 2
    assert json.loads(retained)["env"]["CI"] == "true"  # still readable evidence


async def test_cancelling_the_driver_task_leaves_the_container_alive(deps_fixture):
    """D1: a manager restart must not kill a 60-minute agent run."""
    driver_deps, run_id, fakes = deps_fixture
    fakes.docker.hold_die = True
    task = asyncio.create_task(attend_run(driver_deps, run_id))
    await fakes.docker.started.wait()
    await await_running(fakes, run_id)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not any(c.startswith("kill:") for c in fakes.docker.calls)
    assert not any(c.startswith("remove_container") for c in fakes.docker.calls)
    assert (await fetch(fakes, run_id)).status == "running"


async def test_teardown_scrubs_the_credential_out_of_task_json(deps_fixture):
    """D7: the retained, T8-backed-up run dir must carry no live credential."""
    driver_deps, run_id, fakes = deps_fixture
    fakes.on_started = lambda p: write_outputs(p, status="success")

    await attend_run(driver_deps, run_id)

    payload = json.loads((run_dir_of(fakes, run_id) / "task.json").read_text(encoding="utf-8"))
    assert payload["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "<redacted>"
    assert payload["argv"][0] == "claude"  # still readable evidence
    assert CREDENTIAL not in (run_dir_of(fakes, run_id) / "task.json").read_text(encoding="utf-8")


async def test_a_run_that_is_no_longer_attendable_is_left_alone(deps_fixture):
    """`attend_run` is entered from a sweep that read the row moments ago; by
    the time it runs the row may have moved. It must not clone, launch or
    finalize anything."""
    driver_deps, run_id, fakes = deps_fixture
    async with fakes.factory() as session, session.begin():
        await session.execute(
            text("UPDATE runs SET status = 'canceled', version = version + 1 WHERE id = :r"),
            {"r": run_id},
        )

    await attend_run(driver_deps, run_id)

    assert fakes.docker.calls == []
    assert fakes.git.clone_calls == []


async def test_a_project_with_no_dispatch_config_parks_the_run(deps_fixture, tmp_path):
    """The same `PermanentError` the claim path parks on, one phase later — the
    operator can empty the file between the claim and the attend."""
    driver_deps, run_id, fakes = deps_fixture
    Path(fakes.settings.dispatch_config_file).write_text('{"projects": {}}', encoding="utf-8")

    await attend_run(driver_deps, run_id)

    row = await fetch(fakes, run_id)
    assert (row.status, row.parked_reason) == ("parked", "permanent_error")
    assert fakes.git.clone_calls == []
