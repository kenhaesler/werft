"""SPEC §3.3 item 3 — claim + reservation in one transaction, one candidate per
transaction — and issue #24's "N concurrent claim racers never exceed the
ceiling"."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.integration.test_quota_window import seed_entry
from werft.config.dispatch import DispatchConfig, ProjectDispatch
from werft.config.settings import Settings
from werft.db.models import ProviderAccount, QuotaLedgerEntry, Run, RunAttempt, RunEvent
from werft.domain.runs import run_branch_name
from werft.observe.alerts import NullAlertSink
from werft.orchestrator.dispatch import ClaimOutcome, claim_next
from werft.quota.ledger import LedgerQuota

__all__ = ["seed_entry"]

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
DIGEST = "werft-runner-elastic@sha256:" + "d" * 64
#: The race fixture's account label. Fixed rather than random because the racer
#: sessions are built from a `LedgerQuota` the test constructs itself.
LABEL = "race"


def entry(**over) -> ProjectDispatch:
    return ProjectDispatch(
        image_digest=DIGEST, model="claude-sonnet-4-6", timeout_seconds=1800, **over
    )


def config_for(slug: str, **over) -> DispatchConfig:
    return DispatchConfig(projects={slug: entry(**over)})


async def claim(session, *, quota, config, settings=None, alerts=None, now=NOW, live=0):
    return await claim_next(
        session,
        quota=quota,
        config=config,
        settings=settings or Settings(runs_root="/tmp/werft-runs"),
        alerts=alerts or NullAlertSink(),
        now=now,
        live_driver_count=live,
    )


class SpyAlertSink(NullAlertSink):
    def __init__(self) -> None:
        self.run_parked_calls: list[tuple[str, uuid.UUID, str]] = []

    async def run_parked(self, project_slug: str, run_id: uuid.UUID, reason: str) -> None:
        self.run_parked_calls.append((project_slug, run_id, reason))


# --- seeding (raw SQL, the idiom `tests/integration/test_quota_window.py` uses)
#
# Nothing here returns a *mapped* row for `projects`/`runs`/`provider_accounts`.
# `claim_next` and `resolve_account` read those rows themselves, and an ORM
# instance parked in this session's identity map would shadow the read with a
# pre-`UPDATE` copy — a fixture artefact of sharing one session with the code
# under test, which never happens in production (one session per claim).


async def seed_project(session) -> SimpleNamespace:
    tag = uuid.uuid4().hex[:8]
    project_id = (
        await session.execute(
            text(
                "INSERT INTO projects (slug, github_owner, github_repo)"
                " VALUES (:s, 'ken', :r) RETURNING id"
            ),
            {"s": f"p{tag}", "r": f"repo{tag}"},
        )
    ).scalar_one()
    return SimpleNamespace(id=project_id, slug=f"p{tag}")


async def seed_item(session, project_id, *, number: int = 1) -> uuid.UUID:
    return (
        await session.execute(
            text(
                "INSERT INTO backlog_items (project_id, github_issue_number, title,"
                " github_updated_at) VALUES (:p, :n, 't', now()) RETURNING id"
            ),
            {"p": project_id, "n": number},
        )
    ).scalar_one()


async def seed_queued_run(
    session, project_id, item_id, *, priority: int = 100, created_at: datetime | None = None
) -> uuid.UUID:
    """`next_attempt_at` is always explicit: the column defaults to real
    `now()`, which is in the *future* relative to this module's synthetic
    clock, so a defaulted row would never be a candidate."""
    return (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status, priority,"
                " next_attempt_at, created_at)"
                " VALUES (:p, :i, 'queued', :prio, :nat, :created) RETURNING id"
            ),
            {
                "p": project_id,
                "i": item_id,
                "prio": priority,
                "nat": NOW - timedelta(minutes=1),
                "created": created_at or NOW,
            },
        )
    ).scalar_one()


async def seed_account(session, *, label: str | None = None, ceiling=18000, hours=5):
    label = label or uuid.uuid4().hex[:8]
    account_id = (
        await session.execute(
            text(
                "INSERT INTO provider_accounts (provider, label, rolling_window_hours,"
                " ceiling_seconds) VALUES ('claude', :label, :hours, :ceiling) RETURNING id"
            ),
            {"label": label, "hours": hours, "ceiling": ceiling},
        )
    ).scalar_one()
    return SimpleNamespace(id=account_id, label=label)


async def seed_filler_run(session) -> uuid.UUID:
    """A run that exists only to satisfy `quota_ledger.run_id`'s FK. Its
    `next_attempt_at` is far in the future so it can never become a candidate
    and confuse the test it is seeded for."""
    project = await seed_project(session)
    item = await seed_item(session, project.id)
    return (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status, next_attempt_at)"
                " VALUES (:p, :i, 'queued', :nat) RETURNING id"
            ),
            {"p": project.id, "i": item, "nat": NOW + timedelta(days=365)},
        )
    ).scalar_one()


async def fill_the_window(session, account, *, consumed_at: datetime) -> None:
    """Burn the whole ceiling on one closed entry, so the next reservation can
    only be refused by the *ceiling* rule and wakes on that rule's own time."""
    run_id = await seed_filler_run(session)
    await seed_entry(session, account.id, run_id, 1, reserved=18000, actual=18000, at=consumed_at)


async def insert_closed_attempt(session, run_id, *, attempt_no: int, outcome: str) -> None:
    await session.execute(
        text(
            "INSERT INTO run_attempts (run_id, attempt_no, provider, behavior, outcome,"
            " started_at, ended_at) VALUES (:r, :n, 'claude', 'retry', :o, :s, :e)"
        ),
        {
            "r": run_id,
            "n": attempt_no,
            "o": outcome,
            "s": NOW - timedelta(hours=1),
            "e": NOW - timedelta(minutes=50),
        },
    )


async def seed_another_queued_run(session, project) -> uuid.UUID:
    item = await seed_item(session, project.id, number=uuid.uuid4().int % 100000 + 2)
    return await seed_queued_run(session, project.id, item)


async def seed_race_fixture(session, *, runs: int, slots: int, reservation: int):
    """One project, `runs` claimable rows, and an account whose ceiling admits
    exactly `slots` of them. Committed before the racers start."""
    project = await seed_project(session)
    account_id = (
        await session.execute(
            text(
                "INSERT INTO provider_accounts (provider, label, rolling_window_hours,"
                " ceiling_seconds) VALUES ('claude', :label, 5, :ceiling)"
                " ON CONFLICT (provider, label) DO UPDATE"
                " SET ceiling_seconds = EXCLUDED.ceiling_seconds, is_active = true,"
                " exhausted_until = NULL RETURNING id"
            ),
            {"label": LABEL, "ceiling": slots * reservation},
        )
    ).scalar_one()
    await session.execute(
        text("DELETE FROM quota_ledger WHERE provider_account_id = :a"), {"a": account_id}
    )
    run_ids = [
        await seed_queued_run(session, project.id, await seed_item(session, project.id, number=n))
        for n in range(1, runs + 1)
    ]
    return project.slug, account_id, run_ids


@pytest.fixture
async def seeded(db_session):
    project = await seed_project(db_session)
    item = await seed_item(db_session, project.id)
    run_id = await seed_queued_run(db_session, project.id, item)
    account = await seed_account(db_session)
    quota = LedgerQuota(label=account.label, typical_reservation_seconds=1800)
    return SimpleNamespace(id=run_id), project, account, quota


@pytest.fixture
async def seeded_two(db_session):
    project = await seed_project(db_session)
    low_priority_old = await seed_queued_run(
        db_session,
        project.id,
        await seed_item(db_session, project.id, number=1),
        priority=50,
        created_at=NOW - timedelta(hours=1),
    )
    high_priority_new = await seed_queued_run(
        db_session,
        project.id,
        await seed_item(db_session, project.id, number=2),
        priority=200,
        created_at=NOW,
    )
    account = await seed_account(db_session)
    quota = LedgerQuota(label=account.label, typical_reservation_seconds=1800)
    return low_priority_old, high_priority_new, project, quota


@pytest.fixture
async def seeded_account_only(db_session):
    account = await seed_account(db_session)
    return LedgerQuota(label=account.label, typical_reservation_seconds=1800)


# --- the claim itself --------------------------------------------------------


async def test_claiming_writes_the_lease_branch_attempt_and_ledger_row(db_session, seeded):
    run, project, account, quota = seeded
    outcome = await claim(db_session, quota=quota, config=config_for(project.slug))

    assert (outcome.status, outcome.run_id, outcome.attempt_no) == ("claimed", run.id, 1)

    row = await db_session.get(Run, run.id, populate_existing=True)
    assert row.status == "claimed"
    assert row.provider == "claude"
    assert row.branch_name == run_branch_name(run.id)  # carried note 4
    assert row.runner_image_digest == DIGEST
    assert row.lease_expires_at == NOW + timedelta(seconds=120)
    assert row.hard_deadline_at == NOW + timedelta(seconds=1800 + 600)
    assert (row.container_id, row.exit_code, row.base_sha) == (None, None, None)

    attempt = (
        await db_session.execute(select(RunAttempt).where(RunAttempt.run_id == run.id))
    ).scalar_one()
    assert (attempt.attempt_no, attempt.provider, attempt.behavior, attempt.ended_at) == (
        1,
        "claude",
        "retry",
        None,
    )

    ledger = (
        await db_session.execute(select(QuotaLedgerEntry).where(QuotaLedgerEntry.run_id == run.id))
    ).scalar_one()
    assert (ledger.reserved_wallclock_s, ledger.actual_wallclock_s) == (1800, None)
    assert ledger.consumed_at == NOW  # the supplied clock, never SQL now()
    assert ledger.model == "claude-sonnet-4-6"

    phases = [
        e.payload["phase"]
        for e in (
            await db_session.execute(
                select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.event_type == "dispatch")
            )
        )
        .scalars()
        .all()
    ]
    assert phases == ["claimed"]


async def test_the_branch_name_is_never_spelled_inline(db_session, seeded):
    run, project, _, quota = seeded
    await claim(db_session, quota=quota, config=config_for(project.slug))
    row = await db_session.get(Run, run.id, populate_existing=True)
    assert row.branch_name == f"werft/run-{run.id}"


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE projects SET is_paused = true WHERE id = :p",
        "UPDATE backlog_items SET is_eligible = false WHERE project_id = :p",
    ],
)
async def test_an_ineligible_candidate_is_invisible(db_session, seeded, sql):
    run, project, _, quota = seeded
    await db_session.execute(text(sql), {"p": project.id})
    assert (await claim(db_session, quota=quota, config=config_for(project.slug))).status == "idle"


async def test_a_future_next_attempt_at_is_never_a_candidate(db_session, seeded):
    run, project, _, quota = seeded
    await db_session.execute(
        text("UPDATE runs SET next_attempt_at = :t WHERE id = :r"),
        {"t": NOW + timedelta(minutes=5), "r": run.id},
    )
    assert (await claim(db_session, quota=quota, config=config_for(project.slug))).status == "idle"


async def test_candidates_come_in_priority_then_created_at_order(db_session, seeded_two):
    """Matching `ix_runs_claimable ON runs (priority DESC, created_at)`."""
    low_priority_old, high_priority_new, project, quota = seeded_two
    outcome = await claim(db_session, quota=quota, config=config_for(project.slug))
    assert outcome.run_id == high_priority_new


async def test_a_project_with_no_dispatch_config_parks_that_run(db_session, seeded):
    """SPEC §3.2's `queued -> parked`: "PermanentError pre-attempt (bad config,
    repo 404)". Nothing was reserved and no attempt row exists, so there is
    nothing to true up."""
    run, project, _, quota = seeded
    alerts = SpyAlertSink()

    outcome = await claim(db_session, quota=quota, config=DispatchConfig(), alerts=alerts)

    row = await db_session.get(Run, run.id, populate_existing=True)
    assert outcome.status == "parked"
    assert (row.status, row.parked_reason) == ("parked", "permanent_error")
    assert project.slug in row.error_message and "WERFT_DISPATCH_CONFIG_FILE" in row.error_message
    assert len(alerts.run_parked_calls) == 1
    assert (
        await db_session.execute(
            select(func.count())
            .select_from(QuotaLedgerEntry)
            .where(QuotaLedgerEntry.run_id == run.id)
        )
    ).scalar_one() == 0


async def test_no_active_provider_account_dispatches_nothing_and_parks_nothing(db_session, seeded):
    """D4: a system-wide misconfiguration is not a verdict on any one run, and
    parking the queue would hand the operator a requeue chore the moment they
    fix it."""
    run, project, account, _ = seeded
    outcome = await claim(
        db_session, quota=LedgerQuota(label="absent"), config=config_for(project.slug)
    )
    assert outcome.status == "no_account"
    assert (await db_session.get(Run, run.id, populate_existing=True)).status == "queued"


async def test_a_refused_claim_blocks_with_the_binding_rules_wake_time(db_session, seeded):
    """SPEC §3.2 `queued -> blocked_quota`, floored at now + 60 s so a stale
    `exhausted_until` in the past cannot spin the tick."""
    run, project, account, quota = seeded
    await fill_the_window(db_session, account, consumed_at=NOW - timedelta(hours=4))

    outcome = await claim(db_session, quota=quota, config=config_for(project.slug))

    row = await db_session.get(Run, run.id, populate_existing=True)
    assert outcome.status == "blocked_quota"
    assert row.status == "blocked_quota"
    assert row.next_attempt_at >= NOW + timedelta(seconds=60)
    assert (
        await db_session.execute(
            select(func.count())
            .select_from(QuotaLedgerEntry)
            .where(QuotaLedgerEntry.run_id == run.id)
        )
    ).scalar_one() == 0  # no reservation on a refusal
    assert (
        await db_session.execute(
            select(func.count()).select_from(RunAttempt).where(RunAttempt.run_id == run.id)
        )
    ).scalar_one() == 0  # and no attempt row either


async def test_a_stale_exhausted_until_still_gets_the_sixty_second_floor(db_session, seeded):
    run, project, account, quota = seeded
    await db_session.execute(
        text("UPDATE provider_accounts SET exhausted_until = :t WHERE id = :a"),
        {"t": NOW - timedelta(hours=9), "a": account.id},
    )
    await fill_the_window(db_session, account, consumed_at=NOW)
    await claim(db_session, quota=quota, config=config_for(project.slug))
    row = await db_session.get(Run, run.id, populate_existing=True)
    assert row.next_attempt_at >= NOW + timedelta(seconds=60)


async def test_attempt_no_comes_from_the_cross_table_max_not_attempt_count(db_session, seeded):
    """A budget-exempt attempt (`quota_exhausted`) leaves `attempt_count` at 0
    while both `run_attempts` and `quota_ledger` already hold attempt 1 —
    deriving from `attempt_count` would collide on UNIQUE (run_id, attempt_no)."""
    run, project, account, quota = seeded
    await insert_closed_attempt(db_session, run.id, attempt_no=1, outcome="quota_exhausted")
    await seed_entry(db_session, account.id, run.id, 1, reserved=1800, actual=10, at=NOW)

    outcome = await claim(db_session, quota=quota, config=config_for(project.slug))

    assert outcome.attempt_no == 2  # not 1, and no IntegrityError


async def test_the_concurrency_cap_is_capacity_not_quota_and_changes_no_state(db_session, seeded):
    """D13: `max_concurrent_runs` is the VM-shaped bound. Being at capacity is
    not a verdict on the run — it must not park, block or reserve anything."""
    run, project, _, quota = seeded
    settings = Settings(runs_root="/tmp/werft-runs", max_concurrent_runs=2)

    outcome = await claim(
        db_session, quota=quota, config=config_for(project.slug), settings=settings, live=2
    )

    assert outcome.status == "at_capacity"
    assert (await db_session.get(Run, run.id, populate_existing=True)).status == "queued"


async def test_lowering_the_ceiling_refuses_new_claims_but_never_kills_in_flight_work(
    db_session, seeded
):
    """SPEC §7, final bullet."""
    run, project, account, quota = seeded
    first = await claim(db_session, quota=quota, config=config_for(project.slug))
    assert first.status == "claimed"

    await db_session.execute(
        text("UPDATE provider_accounts SET ceiling_seconds = 1 WHERE id = :a"), {"a": account.id}
    )
    # Raw SQL does not reach the identity map, and the first claim already
    # loaded this account row through it. Production never needs this: each
    # claim transaction gets its own session.
    db_session.expire_all()
    second_run = await seed_another_queued_run(db_session, project)

    second = await claim(db_session, quota=quota, config=config_for(project.slug))

    assert second.status == "blocked_quota"
    assert second.run_id == second_run
    assert (await db_session.get(Run, run.id, populate_existing=True)).status == "claimed"
    assert (
        await db_session.execute(
            select(QuotaLedgerEntry.actual_wallclock_s).where(QuotaLedgerEntry.run_id == run.id)
        )
    ).scalar_one() is None  # the in-flight reservation is untouched


async def test_an_empty_queue_is_idle(db_session, seeded_account_only):
    quota = seeded_account_only
    assert (await claim(db_session, quota=quota, config=DispatchConfig())).status == "idle"


async def test_n_concurrent_racers_never_exceed_the_ceiling(migrated_db):
    """#24, verbatim: "N concurrent claim racers never exceed the ceiling."
    Real sessions, real `FOR UPDATE SKIP LOCKED`, real advisory lock — a single
    session cannot exercise any of that. `SKIP LOCKED` keeps two claimers off
    the same *row*; the advisory lock keeps them off the same *account's
    headroom*. Neither alone passes this test."""
    engine = create_async_engine(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as setup, setup.begin():
            slug, account_id, run_ids = await seed_race_fixture(
                setup, runs=8, slots=3, reservation=1800
            )
        quota = LedgerQuota(label=LABEL, typical_reservation_seconds=1800)
        settings = Settings(runs_root="/tmp/werft-runs", max_concurrent_runs=100)

        async def racer() -> ClaimOutcome:
            async with factory() as session, session.begin():
                return await claim_next(
                    session,
                    quota=quota,
                    config=config_for(slug),
                    settings=settings,
                    alerts=NullAlertSink(),
                    now=NOW,
                    live_driver_count=0,
                )

        outcomes = await asyncio.gather(*(racer() for _ in range(8)))

        async with factory() as check:
            reserved = (
                await check.execute(
                    select(func.coalesce(func.sum(QuotaLedgerEntry.reserved_wallclock_s), 0)).where(
                        QuotaLedgerEntry.provider_account_id == account_id
                    )
                )
            ).scalar_one()
            ceiling = (await check.get(ProviderAccount, account_id)).ceiling_seconds
            statuses = (
                (await check.execute(select(Run.status).where(Run.id.in_(run_ids)))).scalars().all()
            )

        assert reserved <= ceiling, "the ceiling was exceeded — the advisory lock did not serialise"
        assert [o.status for o in outcomes].count("claimed") == 3
        assert statuses.count("claimed") == 3
        assert statuses.count("claimed") + statuses.count("blocked_quota") == 8
    finally:
        await engine.dispose()
