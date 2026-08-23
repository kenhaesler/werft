"""D12: the sweeps are the crash-recovery path. They act only on a row whose
lease has already expired *and* which no live driver owns — with one deliberate
exception, pinned below: `sweep_deadlines` is gated on the live registry only,
because the hard deadline is a ceiling on the run rather than on the driver.

Postgres is real (triggers, CHECK constraints, the quota ledger), Docker is
faked — the daemon is the one surface this milestone cannot exercise in CI.
Every assertion is about a *column*, because columns are the only thing a
manager that has forgotten everything gets to read.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.fakes import FakeDocker, SpyAlerts
from werft.config.settings import Settings
from werft.db.models import Artifact, QuotaLedgerEntry, Run, RunAttempt, RunEvent
from werft.observe.alerts import NullAlertSink
from werft.orchestrator import sweeps as sweeps_module
from werft.orchestrator.sweeps import (
    SweepDeps,
    sweep_canceled_containers,
    sweep_deadlines,
    sweep_leases,
    sweep_orphan_containers,
)
from werft.quota.ledger import LedgerQuota
from werft.runner.docker_api import DockerApiError

#: One fixed instant for the whole module: every sweep takes `now` explicitly,
#: so nothing here depends on how long the test takes.
NOW = datetime.now(UTC).replace(microsecond=0)

#: Sentinel for "the fixture picks a realistic default" — a `running` row always
#: carries a container by construction (the `claimed -> running` CAS sets it),
#: while a `claimed` one may not have started anything yet.
_DEFAULT = object()

#: The path each fixture status is reached by, since the DB trigger enforces
#: SPEC §3.2's edges even for a test's own UPDATE.
_STATUS_PATHS: dict[str, tuple[str, ...]] = {
    "claimed": (),
    "running": ("running",),
    "canceled": ("canceled",),
    "awaiting_review": ("running", "awaiting_review"),
}


# --- fixture -----------------------------------------------------------------


@pytest.fixture
async def sweeps_fixture(migrated_db, db_session, tmp_path):
    """Build one run in whatever in-flight shape a test needs, by claiming it
    for real (attempt row + reservation + lease + deadline) and then editing the
    columns a crash would have left behind."""
    engine = create_async_engine(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    created: list[tuple] = []

    async def build(
        *,
        status: str = "claimed",
        lease_in: timedelta | None = timedelta(minutes=1),
        deadline_in: timedelta | None = timedelta(hours=1),
        attempt_count: int = 0,
        max_attempts: int = 3,
        container_id=_DEFAULT,
        started_minutes_ago: int = 0,
        task_json_with_secret: str | None = None,
    ):
        from werft.config.dispatch import DispatchConfigCache
        from werft.orchestrator.dispatch import claim_next

        tag = uuid.uuid4().hex[:8]
        slug = f"p{tag}"
        # A test that builds a second run does so *after* the first one has been
        # requeued by a sweep, and `claim_next` picks the oldest claimable row in
        # the whole table. Pausing every earlier project keeps "the run this
        # `build()` call returns" the run that was actually claimed here;
        # `is_paused` is a dispatch-side filter and no sweep reads it.
        await db_session.execute(text("UPDATE projects SET is_paused = true"))
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
                " VALUES (:p, :i, 'queued', :due)"
            ),
            # Off `NOW`, not the DB clock: `NOW` is pinned at *import* time, and
            # in a full-suite run the wall clock has moved on by minutes before
            # this executes — `next_attempt_at <= NOW` has to hold anyway.
            {"p": project_id, "i": item_id, "due": NOW - timedelta(minutes=1)},
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

        config_file = tmp_path / f"dispatch-{tag}.json"
        config_file.write_text(
            json.dumps(
                {
                    "projects": {
                        slug: {
                            "image_digest": "werft-runner-elastic@sha256:" + "d" * 64,
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
            dispatch_config_file=str(config_file),
            lease_seconds=120,
            max_concurrent_runs=8,
        )
        config = DispatchConfigCache(str(config_file))
        quota = LedgerQuota(label=account_label, typical_reservation_seconds=1800)

        async with factory() as session, session.begin():
            outcome = await claim_next(
                session,
                quota=quota,
                config=config.current(),
                settings=settings,
                alerts=NullAlertSink(),
                now=NOW,
                live_driver_count=0,
            )
        assert outcome.status == "claimed"
        run_id = outcome.run_id

        cid = ("c1" if status == "running" else None) if container_id is _DEFAULT else container_id

        async with factory() as session, session.begin():
            for step in _STATUS_PATHS[status]:
                await session.execute(
                    text("UPDATE runs SET status = :s, version = version + 1 WHERE id = :r"),
                    {"s": step, "r": run_id},
                )
            await session.execute(
                text(
                    "UPDATE runs SET lease_expires_at = :lease, hard_deadline_at = :deadline,"
                    " attempt_count = :count, max_attempts = :max, container_id = :cid"
                    " WHERE id = :r"
                ),
                {
                    "lease": None if lease_in is None else NOW + lease_in,
                    "deadline": None if deadline_in is None else NOW + deadline_in,
                    "count": attempt_count,
                    "max": max_attempts,
                    "cid": cid,
                    "r": run_id,
                },
            )
            await session.execute(
                text("UPDATE run_attempts SET started_at = :s WHERE run_id = :r"),
                {"s": NOW - timedelta(minutes=started_minutes_ago), "r": run_id},
            )

        run_dir = tmp_path / "runs" / str(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        # Every claimed run has had its secrets mounted; `secrets/` lives inside
        # the tree SPEC §8 retains, so the sweep owes it the same removal the
        # driver's own teardown does (D7).
        (run_dir / "secrets").mkdir(exist_ok=True)
        (run_dir / "secrets" / "git_token").write_text("ghs-test", encoding="utf-8")
        if task_json_with_secret is not None:
            (run_dir / "task.json").write_text(
                json.dumps(
                    {"run_id": str(run_id), "env": {"ANTHROPIC_API_KEY": task_json_with_secret}}
                ),
                encoding="utf-8",
            )

        docker = FakeDocker()
        alerts = SpyAlerts()
        deps = SweepDeps(
            session_factory=factory,
            docker=docker,
            quota=quota,
            alerts=alerts,
            settings=settings,
        )
        fakes = SimpleNamespace(
            docker=docker,
            alerts=alerts,
            factory=factory,
            settings=settings,
            slug=slug,
            runs_root=tmp_path / "runs",
        )
        created.append((deps, run_id, fakes))
        return deps, run_id, fakes

    try:
        yield build
    finally:
        await engine.dispose()


# --- readers -----------------------------------------------------------------


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


def run_dir_of(fakes, run_id):
    return fakes.runs_root / str(run_id)


async def artifacts_of(fakes, run_id) -> list[Artifact]:
    async with fakes.factory() as session:
        return (
            (await session.execute(select(Artifact).where(Artifact.run_id == run_id)))
            .scalars()
            .all()
        )


# --- the tests ---------------------------------------------------------------


async def test_a_live_lease_is_never_touched(sweeps_fixture):
    deps, run_id, fakes = await sweeps_fixture(status="running", lease_in=timedelta(minutes=1))

    assert await sweep_leases(deps, now=NOW, live=set()) == 0
    assert await sweep_deadlines(deps, now=NOW, live=set()) == 0
    assert (await fetch(fakes, run_id)).status == "running"
    assert fakes.docker.calls == []


async def test_a_run_owned_by_a_live_driver_is_skipped_even_with_an_expired_lease(sweeps_fixture):
    """Belt and braces: the lease column is the durable half (after a crash the
    registry is empty), the registry is the in-process half (a driver whose
    heartbeat stalled inside *this* process must not be raced)."""
    deps, run_id, fakes = await sweeps_fixture(status="claimed", lease_in=timedelta(minutes=-1))

    assert await sweep_leases(deps, now=NOW, live={run_id}) == 0
    assert (await fetch(fakes, run_id)).status == "claimed"


async def test_an_expired_claimed_lease_requeues_without_spending_budget(sweeps_fixture):
    """SPEC §3.2's own `claimed -> queued` edge: "lease expired before container
    start". Budget must mean N genuine failures, not N interruptions — so
    `attempt_count` stays put, the attempt row is *closed with NO outcome*
    (never deleted: `MAX(attempt_no)+1` must stay monotone against both unique
    keys), and the reservation is returned in full."""
    deps, run_id, fakes = await sweeps_fixture(
        status="claimed", lease_in=timedelta(minutes=-1), attempt_count=1, container_id="c1"
    )

    assert await sweep_leases(deps, now=NOW, live=set()) == 1

    row = await fetch(fakes, run_id)
    assert (row.status, row.attempt_count) == ("queued", 1)
    assert row.lease_expires_at is None and row.container_id is None
    attempt = await latest_attempt(fakes, run_id)
    assert attempt.ended_at is not None
    assert (attempt.outcome, attempt.duration_seconds) == (None, 0)
    assert (await ledger_entry(fakes, run_id)).actual_wallclock_s == 0  # leak-free
    assert "remove_container:c1" in fakes.docker.calls
    assert (await dispatch_phases(fakes, run_id))[-1] == "lease_expired"


async def test_an_expired_running_lease_fails_the_attempt_as_infra(sweeps_fixture):
    deps, run_id, fakes = await sweeps_fixture(
        status="running", lease_in=timedelta(minutes=-1), started_minutes_ago=7, container_id="c1"
    )

    assert await sweep_leases(deps, now=NOW, live=set()) == 1

    assert (await latest_attempt(fakes, run_id)).outcome == "infra_failure"
    assert (await ledger_entry(fakes, run_id)).actual_wallclock_s == 420  # observed, not reserved
    assert (await fetch(fakes, run_id)).status in ("queued", "parked")  # advance_failed ran
    assert any(c.startswith("kill:") for c in fakes.docker.calls)
    assert (await dispatch_phases(fakes, run_id))[-1] == "lease_expired"


async def test_the_hard_deadline_beats_the_lease_rule_for_the_same_row(sweeps_fixture):
    """D12(c). Both are expired; the run must be a `timeout`, not an
    `infra_failure` — and the lease sweep must leave it alone."""
    deps, run_id, fakes = await sweeps_fixture(
        status="running",
        lease_in=timedelta(minutes=-1),
        deadline_in=timedelta(minutes=-1),
        attempt_count=2,
        max_attempts=3,
        container_id="c1",
    )

    assert await sweep_leases(deps, now=NOW, live=set()) == 0
    assert await sweep_deadlines(deps, now=NOW, live=set()) == 1

    row = await fetch(fakes, run_id)
    assert (row.status, row.parked_reason) == ("parked", "deadline")
    assert (await latest_attempt(fakes, run_id)).outcome == "timeout"
    assert fakes.alerts.run_parked_calls  # SPEC §9
    assert (await dispatch_phases(fakes, run_id))[-1] == "deadline_killed"


async def test_a_canceled_runs_container_is_killed_even_while_a_driver_waits(sweeps_fixture):
    """D10: the driver is blocked in `await_completion` and only the die event
    will free it, so this sweep fires regardless of the registry."""
    deps, run_id, fakes = await sweeps_fixture(status="canceled", container_id="c1")
    outputs = run_dir_of(fakes, run_id) / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "result.json").write_text('{"status": "success"}', encoding="utf-8")

    assert await sweep_canceled_containers(deps) == 1

    assert any(c.startswith("kill:") for c in fakes.docker.calls)
    assert (await dispatch_phases(fakes, run_id))[-1] == "reaped"
    assert await sweep_canceled_containers(deps) == 0  # idempotent

    # T8: evidence collection ran as part of the reap, ahead of the secrets
    # scrub — the two-asserts contract shared with the driver's own teardown.
    artifacts = await artifacts_of(fakes, run_id)
    assert any(a.path.startswith("outputs/") for a in artifacts)
    assert "artifacts" in await dispatch_phases(fakes, run_id)


async def test_an_evidence_collection_blowup_still_lets_the_secrets_scrub_run(
    sweeps_fixture, monkeypatch
):
    """Same failure-isolation contract as the driver's teardown: a
    `collect_run_evidence` blow-up must not stop the secrets scrub or escape
    `reap_run_containers`."""
    deps, run_id, fakes = await sweeps_fixture(status="canceled", container_id="c1")
    token = run_dir_of(fakes, run_id) / "secrets" / "git_token"
    assert token.exists()

    async def blows_up(*_args, **_kwargs):
        raise RuntimeError("evidence collection exploded")

    monkeypatch.setattr(sweeps_module, "collect_run_evidence", blows_up)

    assert await sweep_canceled_containers(deps) == 1  # must not raise

    assert not token.exists()
    assert (await dispatch_phases(fakes, run_id))[-1] == "reaped"


async def test_the_orphan_sweep_reaps_terminal_runs_that_still_have_a_container(sweeps_fixture):
    """Crash-window row 7: finalize committed, teardown never ran."""
    deps, run_id, fakes = await sweeps_fixture(status="awaiting_review", container_id="c1")
    fakes.docker.containers = [
        {
            "Id": "c1",
            "Labels": {"werft.run_id": str(run_id)},
            "Names": [f"/werft-run-{run_id}"],
        }
    ]

    assert await sweep_orphan_containers(deps, live=set()) == 1

    assert "remove_container:c1" in fakes.docker.calls
    assert f"remove_network:werft-net-{run_id}" in fakes.docker.calls
    assert (await dispatch_phases(fakes, run_id))[-1] == "reaped"
    assert await sweep_orphan_containers(deps, live=set()) == 0  # idempotent


async def test_the_orphan_sweep_scrubs_the_credential_from_task_json(sweeps_fixture):
    """D7: the paths no driver survived still must not leave a live token in a
    tree SPEC §8 retains and ships offsite."""
    deps, run_id, fakes = await sweeps_fixture(
        status="canceled", container_id="c1", task_json_with_secret="sk-test"
    )
    fakes.docker.containers = [{"Id": "c1", "Labels": {"werft.run_id": str(run_id)}, "Names": []}]

    await sweep_orphan_containers(deps, live=set())

    task_json = (run_dir_of(fakes, run_id) / "task.json").read_text(encoding="utf-8")
    assert "<redacted>" in task_json
    assert "sk-test" not in task_json


async def test_the_orphan_sweep_leaves_live_runs_alone(sweeps_fixture):
    deps, run_id, fakes = await sweeps_fixture(
        status="running", lease_in=timedelta(minutes=5), container_id="c1"
    )
    fakes.docker.containers = [{"Id": "c1", "Labels": {"werft.run_id": str(run_id)}, "Names": []}]

    assert await sweep_orphan_containers(deps, live=set()) == 0


async def test_an_unreachable_daemon_is_not_a_sweep_failure(sweeps_fixture):
    """Crash-window row 5: the manager must still recover *rows* when the
    daemon is down; the container is found again next tick."""
    deps, run_id, fakes = await sweeps_fixture(
        status="claimed", lease_in=timedelta(minutes=-1), container_id="c1"
    )
    fakes.docker.error = DockerApiError(500, "connection refused")

    assert await sweep_leases(deps, now=NOW, live=set()) == 1  # the row still moves
    assert (await fetch(fakes, run_id)).status == "queued"
    assert await sweep_orphan_containers(deps, live=set()) == 0  # and this just logs


async def test_a_daemon_error_mid_reap_never_retires_the_canceled_container(sweeps_fixture):
    """`reap_run_containers` never raises, so the *only* thing separating "the
    container is gone" from "a 500 ate the request" is its return value. Record
    intent instead of success and one transient outage disables this sweep for
    the run forever — the container and its network leak permanently, in exactly
    the genuine-outage case the sweep exists for (`docker_api` already swallows
    404/409)."""
    deps, run_id, fakes = await sweeps_fixture(status="canceled", container_id="c1")
    fakes.docker.error = DockerApiError(500, "connection refused")

    assert await sweep_canceled_containers(deps) == 0
    assert "reaped" not in await dispatch_phases(fakes, run_id)

    fakes.docker.error = None
    assert await sweep_canceled_containers(deps) == 1  # still a candidate
    assert (await dispatch_phases(fakes, run_id))[-1] == "reaped"


async def test_a_daemon_error_mid_reap_never_retires_the_orphan_container(sweeps_fixture):
    """Same half for the orphan sweep: the scan succeeds, the remove 500s. The
    container is still listed, so the next tick must still see it."""
    deps, run_id, fakes = await sweeps_fixture(status="awaiting_review", container_id="c1")
    fakes.docker.containers = [{"Id": "c1", "Labels": {"werft.run_id": str(run_id)}, "Names": []}]
    fakes.docker.error = DockerApiError(500, "connection refused")
    fakes.docker.failing_ops = {"kill_container", "remove_container", "remove_network"}

    assert await sweep_orphan_containers(deps, live=set()) == 0
    assert "reaped" not in await dispatch_phases(fakes, run_id)

    fakes.docker.error = None
    assert await sweep_orphan_containers(deps, live=set()) == 1
    assert (await dispatch_phases(fakes, run_id))[-1] == "reaped"


async def test_a_later_attempts_orphan_container_is_still_reaped(sweeps_fixture):
    """The other half of the same finding: `queued` is in the orphan sweep's
    candidate set and is retryable, so a run reaped once must not become
    unreapable. Keying cleanup on the *run* would strand every subsequent
    attempt's container."""
    deps, run_id, fakes = await sweeps_fixture(
        status="claimed", lease_in=timedelta(minutes=-1), container_id="c1"
    )
    fakes.docker.containers = [{"Id": "c1", "Labels": {"werft.run_id": str(run_id)}, "Names": []}]

    assert await sweep_leases(deps, now=NOW, live=set()) == 1  # -> queued, c1 removed
    assert await sweep_orphan_containers(deps, live=set()) == 0  # nothing left to reap

    # The next attempt crashes the same way and leaves its own container behind.
    fakes.docker.containers = [{"Id": "c2", "Labels": {"werft.run_id": str(run_id)}, "Names": []}]

    assert await sweep_orphan_containers(deps, live=set()) == 1
    assert "remove_container:c2" in fakes.docker.calls


async def test_the_sweep_removes_the_mounted_secret_files_too(sweeps_fixture):
    """D7 / SPEC §10: `secrets_dir` is `run_dir/secrets`, inside the tree SPEC §8
    retains and ships offsite. Scrubbing `task.json` and leaving the mounted
    token file behind upholds nothing. `revoke()` is the one piece this path
    genuinely cannot do — it needs the credential object that died with the
    driver — but `remove_secrets` has no such dependency."""
    deps, run_id, fakes = await sweeps_fixture(status="canceled", container_id="c1")
    token = run_dir_of(fakes, run_id) / "secrets" / "git_token"
    assert token.exists()

    assert await sweep_canceled_containers(deps) == 1

    assert not token.exists()
    assert (run_dir_of(fakes, run_id) / "secrets").is_dir()  # the tree itself is retained


async def test_a_blown_deadline_is_acted_on_even_while_the_lease_is_still_valid(sweeps_fixture):
    """The module docstring's arbitration rule has exactly one exception, and
    this pins it: the deadline sweep is gated on the live registry only. A
    crashed manager leaves a row whose lease is valid for up to `lease_seconds`
    while its ceiling has already blown; waiting the lease out there just burns
    the ceiling the deadline exists to enforce."""
    deps, run_id, fakes = await sweeps_fixture(
        status="running",
        lease_in=timedelta(minutes=5),
        deadline_in=timedelta(minutes=-1),
        started_minutes_ago=7,
        container_id="c1",
    )

    assert await sweep_leases(deps, now=NOW, live=set()) == 0
    assert await sweep_deadlines(deps, now=NOW, live=set()) == 1

    assert (await latest_attempt(fakes, run_id)).outcome == "timeout"
    assert (await dispatch_phases(fakes, run_id))[-1] == "deadline_killed"
    # ...and the registry is still honoured: a live driver keeps its own row.
    deps2, run2, _ = await sweeps_fixture(
        status="running", lease_in=timedelta(minutes=5), deadline_in=timedelta(minutes=-1)
    )
    assert await sweep_deadlines(deps2, now=NOW, live={run2}) == 0


async def test_every_sweep_settles_the_reservation_in_the_transitions_own_transaction(
    sweeps_fixture,
):
    """SPEC §7: "no path leaks headroom." Split the true-up from the CAS and a
    crash between the two leaves either a released reservation on a still-
    claimed run (over-admits) or a claimed run whose reservation nobody will
    ever close."""
    for status, expected in (("claimed", 0), ("running", 420)):
        deps, run_id, fakes = await sweeps_fixture(
            status=status, lease_in=timedelta(minutes=-1), started_minutes_ago=7
        )
        await sweep_leases(deps, now=NOW, live=set())
        assert (await ledger_entry(fakes, run_id)).actual_wallclock_s == expected, status
