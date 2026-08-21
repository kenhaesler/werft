"""D9: the blocked_quota wake is computed from the window, not guessed at 15
minutes, whenever the ledger can answer. Carried note 3: `advance_failed`'s
reserved `quota` parameter is what carries it, and its signature does not
move."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from tests.integration.test_finalize import SpyAlertSink
from werft.db.models import ProviderAccount, Run
from werft.db.transitions import transition_run
from werft.domain.attempts import AttemptOutcome
from werft.domain.runs import RunStatus
from werft.orchestrator.finalize import NullQuota, advance_failed
from werft.quota.ledger import LedgerQuota

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


async def _seed_project_and_item(session) -> tuple[uuid.UUID, uuid.UUID]:
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
    item_id = (
        await session.execute(
            text(
                "INSERT INTO backlog_items (project_id, github_issue_number, title,"
                " github_updated_at) VALUES (:p, 1, 't', now()) RETURNING id"
            ),
            {"p": project_id},
        )
    ).scalar_one()
    return project_id, item_id


async def seed_failed_run(
    session, *, attempt_count: int = 0, max_attempts: int = 3, provider: str = "claude"
) -> Run:
    """A project + backlog item + run driven `queued -> claimed -> running ->
    failed` through `transition_run`, matching the only path a real run
    reaches `failed` by."""
    project_id, item_id = await _seed_project_and_item(session)
    run_id = (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status, provider,"
                " attempt_count, max_attempts) VALUES (:p, :i, 'queued', :prov, :ac, :ma)"
                " RETURNING id"
            ),
            {
                "p": project_id,
                "i": item_id,
                "prov": provider,
                "ac": attempt_count,
                "ma": max_attempts,
            },
        )
    ).scalar_one()
    await session.commit()
    run = await session.get(Run, run_id)
    for status in (RunStatus.CLAIMED, RunStatus.RUNNING, RunStatus.FAILED):
        ok = await transition_run(
            session, run_id=run.id, expected_version=run.version, new_status=status
        )
        assert ok
        run = await session.get(Run, run.id, populate_existing=True)
    await session.commit()
    return run


async def seed_failed_run_with_full_window(session) -> tuple[Run, ProviderAccount]:
    """An active account (`ceiling_seconds=3600`, `rolling_window_hours=5`)
    with one closed 3600 s ledger entry consumed 4 h before `datetime.now(UTC)`
    — a full window that only ages out, one hour from now, once that entry
    falls outside the rolling window."""
    label = uuid.uuid4().hex[:8]
    account_id = (
        await session.execute(
            text(
                "INSERT INTO provider_accounts (provider, label, rolling_window_hours,"
                " ceiling_seconds) VALUES ('claude', :label, 5, 3600) RETURNING id"
            ),
            {"label": label},
        )
    ).scalar_one()
    run = await seed_failed_run(session)
    await session.execute(
        text(
            "INSERT INTO quota_ledger (provider_account_id, run_id, attempt_no,"
            " reserved_wallclock_s, actual_wallclock_s, consumed_at)"
            " VALUES (:a, :r, 1, 3600, 3600, :at)"
        ),
        {"a": account_id, "r": run.id, "at": datetime.now(UTC) - timedelta(hours=4)},
    )
    await session.commit()
    account = await session.get(ProviderAccount, account_id)
    return run, account


async def test_null_quota_keeps_the_t5_contract(db_session):
    reported = NOW + timedelta(hours=1)
    got = await NullQuota().next_wake_at(db_session, None, reported)
    assert got == reported
    unreported = await NullQuota().next_wake_at(db_session, None, None)
    assert timedelta(minutes=14) < unreported - datetime.now(UTC) < timedelta(minutes=16)


async def test_the_wake_comes_from_the_port_not_a_fixed_fifteen_minutes(db_session):
    """The in-window entry was consumed 4 h ago against a 5 h window and a
    ceiling only it fills: the wake is when it ages out, ~1 h from now."""
    run, account = await seed_failed_run_with_full_window(db_session)

    await advance_failed(
        db_session,
        run,
        outcome=AttemptOutcome.QUOTA_EXHAUSTED,
        exhausted_until=None,
        quota=LedgerQuota(label=account.label, typical_reservation_seconds=1800),
        alerts=SpyAlertSink(),
    )

    refreshed = await db_session.get(Run, run.id, populate_existing=True)
    assert refreshed.status == "blocked_quota"
    wait = refreshed.next_attempt_at - datetime.now(UTC)
    assert timedelta(minutes=55) < wait < timedelta(minutes=65)


async def test_a_provider_reported_reset_wins_and_is_recorded_durably(db_session):
    run, account = await seed_failed_run_with_full_window(db_session)
    reported = datetime.now(UTC) + timedelta(hours=3)
    alerts = SpyAlertSink()

    await advance_failed(
        db_session,
        run,
        outcome=AttemptOutcome.QUOTA_EXHAUSTED,
        exhausted_until=reported,
        quota=LedgerQuota(label=account.label),
        alerts=alerts,
    )

    refreshed = await db_session.get(Run, run.id, populate_existing=True)
    assert refreshed.next_attempt_at == reported
    stored = await db_session.get(ProviderAccount, account.id, populate_existing=True)
    assert stored.exhausted_until == reported  # durable (D11), not just a run column
    assert alerts.quota_exhausted_until_calls == [("claude", reported)]


async def test_a_computed_wake_fires_no_alert(db_session):
    """The 15-minute-or-headroom time is this system's arithmetic, not a fact
    the provider reported. Alerting it would be inventing information."""
    run, account = await seed_failed_run_with_full_window(db_session)
    alerts = SpyAlertSink()
    await advance_failed(
        db_session,
        run,
        outcome=AttemptOutcome.QUOTA_EXHAUSTED,
        exhausted_until=None,
        quota=LedgerQuota(label=account.label),
        alerts=alerts,
    )
    assert alerts.quota_exhausted_until_calls == []


async def test_a_deadline_park_says_deadline_not_agent_failure(db_session):
    """D12(c): `parked_reason` has a `deadline` slot; using `agent_failure`
    would point the operator at the prompt when the wall clock ran out."""
    run = await seed_failed_run(db_session, attempt_count=2, max_attempts=3)
    await advance_failed(
        db_session,
        run,
        outcome=AttemptOutcome.TIMEOUT,
        exhausted_until=None,
        quota=NullQuota(),
        alerts=SpyAlertSink(),
    )
    refreshed = await db_session.get(Run, run.id, populate_existing=True)
    assert (refreshed.status, refreshed.parked_reason) == ("parked", "deadline")
