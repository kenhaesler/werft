"""`quota/ledger.py` against a real DB (SPEC §7; plan decisions D2, D4, D9, D11).

Seeding reuses `test_quota_window.py`'s `seed_run`/`seed_entry`/`NOW` (the same
idiom `test_finalize.py` and `test_quota_window.py` already use: raw-SQL
inserts, then real model instances read back through the ORM). `seed_account`
there mints a random label per call, which is exactly wrong for these tests —
`LedgerQuota` needs a *known* label to resolve against — so this module adds
its own `seed_labeled_account` rather than reusing it.

`next_wake_at` takes its own `datetime.now(UTC)` internally (it runs inside
`advance_failed`'s transaction, where the real clock is the right one), so
tests that exercise it capture a real "before" timestamp and assert windows,
never equality against the frozen `NOW` — except where the account is rigged
so the provider-reported value is the only candidate that can ever win (an
open reservation alone already exceeds the ceiling, so headroom-by-ageing is
unreachable and returns `None`), in which case equality against the reported
value is exact and clock-independent.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from tests.integration.test_quota_window import NOW, seed_entry, seed_run
from werft.db.models import ProviderAccount, QuotaLedgerEntry, Run
from werft.quota.ledger import LedgerQuota, QuotaRefused


async def seed_labeled_account(session, *, label, ceiling=18000, hours=5) -> uuid.UUID:
    return (
        await session.execute(
            text(
                "INSERT INTO provider_accounts (provider, label, rolling_window_hours,"
                " ceiling_seconds) VALUES ('claude', :label, :hours, :ceiling) RETURNING id"
            ),
            {"label": label, "hours": hours, "ceiling": ceiling},
        )
    ).scalar_one()


async def insert_attempt(session, run_id, attempt_no, *, provider="claude") -> None:
    await session.execute(
        text("INSERT INTO run_attempts (run_id, attempt_no, provider) VALUES (:r, :n, :p)"),
        {"r": run_id, "n": attempt_no, "p": provider},
    )


async def entry(session, run_id) -> QuotaLedgerEntry:
    return (
        await session.execute(
            select(QuotaLedgerEntry)
            .where(QuotaLedgerEntry.run_id == run_id)
            .order_by(QuotaLedgerEntry.attempt_no.desc())
            .limit(1)
        )
    ).scalar_one()


async def get_run(session, run_id) -> Run:
    return await session.get(Run, run_id)


async def ledger_count(session) -> int:
    return (await session.execute(select(func.count()).select_from(QuotaLedgerEntry))).scalar_one()


async def test_reserve_writes_one_open_row_with_the_supplied_clock(db_session):
    label = uuid.uuid4().hex[:8]
    account_id = await seed_labeled_account(db_session, label=label)
    run_id = await seed_run(db_session)
    ledger = LedgerQuota(label=label)

    account = await ledger.lock_and_resolve(db_session)
    assert account is not None and account.id == account_id
    reservation = await ledger.reserve(
        db_session,
        account=account,
        run_id=run_id,
        attempt_no=1,
        model="claude-sonnet-4-6",
        reservation_seconds=5400,
        now=NOW,
    )
    assert reservation.reserved_seconds == 5400
    row = await entry(db_session, run_id)
    assert (row.reserved_wallclock_s, row.actual_wallclock_s) == (5400, None)
    assert row.consumed_at == NOW  # explicit, never the column default


async def test_reserve_raises_quota_refused_with_the_binding_rules_wake_time(db_session):
    label = uuid.uuid4().hex[:8]
    # A ceiling smaller than the reservation itself: no amount of ageing can
    # ever make it fit, so this is unambiguously the "ceiling" rule.
    await seed_labeled_account(db_session, label=label, ceiling=1000)
    run_id = await seed_run(db_session)
    ledger = LedgerQuota(label=label)
    account = await ledger.lock_and_resolve(db_session)

    with pytest.raises(QuotaRefused) as caught:
        await ledger.reserve(
            db_session,
            account=account,
            run_id=run_id,
            attempt_no=1,
            model="claude-sonnet-4-6",
            reservation_seconds=5400,
            now=NOW,
        )
    assert caught.value.reason == "ceiling"
    assert caught.value.retry_at > NOW
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(QuotaLedgerEntry)
            .where(QuotaLedgerEntry.run_id == run_id)
        )
    ).scalar_one()
    assert count == 0  # no row on refusal


async def test_lock_and_resolve_is_none_when_no_active_account_exists(db_session):
    assert await LedgerQuota(label="absent").lock_and_resolve(db_session) is None


async def test_next_attempt_no_is_the_max_over_BOTH_tables_plus_one(db_session):
    """`quota_ledger` and `run_attempts` each carry UNIQUE(run_id, attempt_no),
    and a lease expiry keeps both rows, so the counter must clear the higher of
    the two or the next claim's INSERT explodes."""
    label = uuid.uuid4().hex[:8]
    account_id = await seed_labeled_account(db_session, label=label)
    run_id = await seed_run(db_session)
    ledger = LedgerQuota(label=label)

    assert await ledger.next_attempt_no(db_session, run_id) == 1
    await insert_attempt(db_session, run_id, attempt_no=1)
    assert await ledger.next_attempt_no(db_session, run_id) == 2
    await seed_entry(db_session, account_id, run_id, 5, reserved=1, actual=1, at=NOW)
    assert await ledger.next_attempt_no(db_session, run_id) == 6


async def test_true_up_is_first_writer_wins(db_session):
    """Cancel racing finalize: whoever transitions the run first owns the
    number. A second true-up must be a no-op, or the two of them double-count
    (or worse, the later one rewrites an already-settled reservation)."""
    label = uuid.uuid4().hex[:8]
    account_id = await seed_labeled_account(db_session, label=label)
    run_id = await seed_run(db_session)
    await seed_entry(db_session, account_id, run_id, 1, reserved=5400, actual=None, at=NOW)
    ledger = LedgerQuota(label=label)

    assert await ledger.true_up(db_session, run_id=run_id, attempt_no=1, observed_seconds=120)
    assert not await ledger.true_up(db_session, run_id=run_id, attempt_no=1, observed_seconds=999)
    assert (await entry(db_session, run_id)).actual_wallclock_s == 120


async def test_release_closes_the_newest_open_row_without_being_told_its_number(db_session):
    """`QuotaPort.release`'s signature carries no attempt_no (its callers
    predate T7), so `release` resolves it — the newest open row for this run —
    and applies the identical `actual_wallclock_s IS NULL` guard."""
    label = uuid.uuid4().hex[:8]
    account_id = await seed_labeled_account(db_session, label=label)
    run_id = await seed_run(db_session)
    await seed_entry(
        db_session, account_id, run_id, 1, reserved=5400, actual=300, at=NOW - timedelta(hours=1)
    )
    await seed_entry(db_session, account_id, run_id, 2, reserved=5400, actual=None, at=NOW)
    run = await get_run(db_session, run_id)
    ledger = LedgerQuota(label=label)

    await ledger.release(db_session, run, 300)
    row = await entry(db_session, run_id)
    assert row.attempt_no == 2
    assert row.actual_wallclock_s == 300


async def test_release_of_none_records_zero_not_null(db_session):
    label = uuid.uuid4().hex[:8]
    account_id = await seed_labeled_account(db_session, label=label)
    run_id = await seed_run(db_session)
    await seed_entry(db_session, account_id, run_id, 1, reserved=5400, actual=None, at=NOW)
    run = await get_run(db_session, run_id)
    ledger = LedgerQuota(label=label)

    await ledger.release(db_session, run, None)
    assert (await entry(db_session, run_id)).actual_wallclock_s == 0


async def test_release_on_a_run_with_no_reservation_is_a_no_op(db_session):
    """T5-era runs, and any finalize re-drive after a cancel already closed the
    row, must not raise inside somebody else's transaction."""
    label = uuid.uuid4().hex[:8]
    await seed_labeled_account(db_session, label=label)
    run_without_ledger = await get_run(db_session, await seed_run(db_session))

    await LedgerQuota(label=label).release(db_session, run_without_ledger, 10)


async def test_release_is_uncapped_and_records_what_the_manager_observed(db_session):
    """SPEC §7 meters wall clock. A run that overran its reservation reports
    the overrun — honesty over neatness; capping would under-report the
    window."""
    label = uuid.uuid4().hex[:8]
    account_id = await seed_labeled_account(db_session, label=label)
    run_id = await seed_run(db_session)
    await seed_entry(db_session, account_id, run_id, 1, reserved=5400, actual=None, at=NOW)
    run = await get_run(db_session, run_id)
    ledger = LedgerQuota(label=label)

    await ledger.release(db_session, run, 9999)  # reserved 5400
    assert (await entry(db_session, run_id)).actual_wallclock_s == 9999


async def test_next_wake_at_prefers_a_provider_reported_reset_and_records_it(db_session):
    label = uuid.uuid4().hex[:8]
    account_id = await seed_labeled_account(db_session, label=label, ceiling=18000)
    # An open reservation big enough that headroom-by-ageing can never fit the
    # typical reservation, however much closed usage ages out: the provider
    # report is the only candidate `next_wake_at` can return.
    open_run = await seed_run(db_session)
    await seed_entry(db_session, account_id, open_run, 1, reserved=15000, actual=None, at=NOW)
    run = await get_run(db_session, await seed_run(db_session))
    ledger = LedgerQuota(label=label)

    reported = NOW + timedelta(hours=2)
    got = await ledger.next_wake_at(db_session, run, reported)
    assert got == reported
    account = await db_session.get(ProviderAccount, account_id, populate_existing=True)
    assert (account.exhausted_until, account.exhausted_source) == (reported, "cli")


async def test_a_later_report_wins_and_an_earlier_one_never_shortens_it(db_session):
    """A stale signal must never shorten a live one (D9)."""
    label = uuid.uuid4().hex[:8]
    account_id = await seed_labeled_account(db_session, label=label, ceiling=18000)
    open_run = await seed_run(db_session)
    await seed_entry(db_session, account_id, open_run, 1, reserved=15000, actual=None, at=NOW)
    run = await get_run(db_session, await seed_run(db_session))
    ledger = LedgerQuota(label=label)

    late = NOW + timedelta(hours=3)
    await ledger.next_wake_at(db_session, run, late)
    await ledger.next_wake_at(db_session, run, NOW + timedelta(hours=1))
    account = await db_session.get(ProviderAccount, account_id, populate_existing=True)
    assert account.exhausted_until == late


async def test_next_wake_at_without_a_reset_blocks_for_fifteen_minutes_and_says_so(db_session):
    """D11: refusing to block at all would re-burn the account on the next
    tick; the source string is what keeps the invented number visible."""
    label = uuid.uuid4().hex[:8]
    account_id = await seed_labeled_account(db_session, label=label, ceiling=18000)
    run = await get_run(db_session, await seed_run(db_session))
    ledger = LedgerQuota(label=label)

    before = datetime.now(UTC)
    got = await ledger.next_wake_at(db_session, run, None)
    account = await db_session.get(ProviderAccount, account_id, populate_existing=True)
    assert account.exhausted_source == "cli_no_reset"
    assert timedelta(minutes=14) < account.exhausted_until - before < timedelta(minutes=16)
    assert got >= account.exhausted_until


async def test_next_wake_at_is_when_the_oldest_in_window_entry_ages_out(db_session):
    """Ceiling 18000, one closed 9000 s entry 4 h old, one open 9000 s
    reservation: a typical reservation fits only once the closed entry ages
    out, one hour from `now` (plus the boundary second)."""
    label = uuid.uuid4().hex[:8]
    account_id = await seed_labeled_account(db_session, label=label, ceiling=18000, hours=5)
    real_now = datetime.now(UTC)
    closed_run = await seed_run(db_session)
    open_run = await seed_run(db_session)
    await seed_entry(
        db_session,
        account_id,
        closed_run,
        1,
        reserved=9000,
        actual=9000,
        at=real_now - timedelta(hours=4),
    )
    await seed_entry(db_session, account_id, open_run, 1, reserved=9000, actual=None, at=real_now)
    run = await get_run(db_session, await seed_run(db_session))
    ledger = LedgerQuota(label=label)

    wake = await ledger.next_wake_at(db_session, run, None)
    assert timedelta(minutes=55) < wake - real_now < timedelta(minutes=65)


async def test_a_recorded_reading_writes_account_metadata_and_no_ledger_row(db_session):
    label = uuid.uuid4().hex[:8]
    account_id = await seed_labeled_account(db_session, label=label)
    ledger = LedgerQuota(label=label)

    await ledger.record_reading(
        db_session, account_id=account_id, utilization_percent=10.0, source="usage", at=NOW
    )
    account = await db_session.get(ProviderAccount, account_id, populate_existing=True)
    assert (float(account.last_reading_utilization), account.last_reading_source) == (
        10.0,
        "usage",
    )
    assert await ledger_count(db_session) == 0
