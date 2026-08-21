"""SPEC §7's window, and the parity that makes displayed headroom the same
number as enforced headroom (plan decision D8)."""

import uuid
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from tests.integration.test_api_runs import auth_headers, make_client_app, token_file
from werft.quota.window import (
    ClosedEntry,
    WindowUsage,
    earliest_headroom_at,
    read_window,
)

__all__ = ["auth_headers", "make_client_app", "token_file"]

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


async def seed_account(session, *, ceiling=18000, hours=5) -> uuid.UUID:
    return (
        await session.execute(
            text(
                "INSERT INTO provider_accounts (provider, label, rolling_window_hours,"
                " ceiling_seconds) VALUES ('claude', :label, :hours, :ceiling) RETURNING id"
            ),
            {"label": uuid.uuid4().hex[:8], "hours": hours, "ceiling": ceiling},
        )
    ).scalar_one()


async def seed_run(session) -> uuid.UUID:
    """A project + backlog item + run; `quota_ledger.run_id` is a FK.
    (Raw-SQL seeding, the idiom `tests/integration/test_loop.py` already uses.)"""
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
    return (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status)"
                " VALUES (:p, :i, 'queued') RETURNING id"
            ),
            {"p": project_id, "i": item_id},
        )
    ).scalar_one()


async def seed_entry(session, account_id, run_id, attempt_no, *, reserved, actual, at):
    await session.execute(
        text(
            "INSERT INTO quota_ledger (provider_account_id, run_id, attempt_no,"
            " reserved_wallclock_s, actual_wallclock_s, consumed_at)"
            " VALUES (:a, :r, :n, :res, :act, :at)"
        ),
        {"a": account_id, "r": run_id, "n": attempt_no, "res": reserved, "act": actual, "at": at},
    )


async def test_closed_entries_age_out_at_the_rolling_boundary(db_session):
    account_id = await seed_account(db_session, hours=5)
    inside, outside = await seed_run(db_session), await seed_run(db_session)
    await seed_entry(
        db_session, account_id, inside, 1, reserved=5400, actual=600, at=NOW - timedelta(minutes=1)
    )
    await seed_entry(
        db_session,
        account_id,
        outside,
        1,
        reserved=5400,
        actual=900,
        at=NOW - timedelta(hours=5, minutes=1),
    )

    usage = await read_window(db_session, account_id, now=NOW)

    assert usage.consumed_seconds == 600  # the 900 s entry aged out
    assert usage.reserved_seconds == 0
    assert usage.entry_count == 1
    assert usage.oldest_in_window_at == NOW - timedelta(minutes=1)


async def test_open_reservations_never_age_out(db_session):
    """An unresolved reservation is work Werft believes is in flight. Ageing
    it out would hand its seconds back while the container is still burning
    them — the same leak SPEC §7 rules out from the other direction."""
    account_id = await seed_account(db_session, hours=5)
    old_open = await seed_run(db_session)
    await seed_entry(
        db_session, account_id, old_open, 1, reserved=5400, actual=None, at=NOW - timedelta(hours=9)
    )

    usage = await read_window(db_session, account_id, now=NOW)

    assert usage.reserved_seconds == 5400
    assert usage.consumed_seconds == 0


async def test_an_empty_account_reads_zeros_not_none(db_session):
    account_id = await seed_account(db_session)
    assert await read_window(db_session, account_id, now=NOW) == WindowUsage(0, 0, 0, None)


async def test_the_window_moves_with_the_supplied_now_and_nothing_else(db_session):
    """The synthetic clock, in one assertion: the same rows, two `now`s."""
    account_id = await seed_account(db_session, hours=5)
    run_id = await seed_run(db_session)
    await seed_entry(
        db_session,
        account_id,
        run_id,
        1,
        reserved=900,
        actual=900,
        at=NOW - timedelta(hours=4, minutes=59),
    )

    assert (await read_window(db_session, account_id, now=NOW)).consumed_seconds == 900
    later = NOW + timedelta(minutes=2)
    assert (await read_window(db_session, account_id, now=later)).consumed_seconds == 0


async def test_the_quota_endpoint_reads_the_same_numbers_admission_enforces(
    db_session, token_file, auth_headers
):
    """D8's parity, asserted directly: whatever `/api/v1/quota` prints as
    consumed/reserved is what `read_window` hands the admission rule.

    Seeded relative to real wall-clock time (not the fixed `NOW` constant):
    the endpoint under test calls `datetime.now(UTC)` itself, so the rows
    must sit inside *that* clock's window for the comparison to mean
    anything.
    """
    real_now = datetime.now(UTC)
    account_id = await seed_account(db_session, hours=5, ceiling=18000)
    run1, run2 = await seed_run(db_session), await seed_run(db_session)
    await seed_entry(
        db_session,
        account_id,
        run1,
        1,
        reserved=100,
        actual=120,
        at=real_now - timedelta(minutes=1),
    )
    await seed_entry(
        db_session,
        account_id,
        run2,
        1,
        reserved=50,
        actual=None,
        at=real_now - timedelta(minutes=1),
    )
    await db_session.commit()

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/quota", headers=auth_headers)
    assert resp.status_code == 200
    acct = next(a for a in resp.json()["accounts"] if a["ceiling_seconds"] == 18000)

    usage = await read_window(db_session, account_id, now=datetime.now(UTC))
    assert acct["consumed_seconds"] == usage.consumed_seconds
    assert acct["reserved_seconds"] == usage.reserved_seconds


def test_earliest_headroom_is_now_when_the_reservation_already_fits():
    usage = WindowUsage(600, 0, 1, NOW - timedelta(minutes=10))
    assert (
        earliest_headroom_at(
            [ClosedEntry(NOW - timedelta(minutes=10), 600)],
            usage=usage,
            now=NOW,
            window_hours=5,
            ceiling_seconds=3600,
            reservation_seconds=1800,
        )
        == NOW
    )


def test_earliest_headroom_is_one_second_past_the_oldest_entrys_boundary():
    """The predicate is `consumed_at >= floor`, so *at* `consumed_at + window`
    the row is still counted. The wake must be the first instant it is not."""
    oldest = NOW - timedelta(hours=4, minutes=50)
    closed = [ClosedEntry(oldest, 1800), ClosedEntry(NOW - timedelta(hours=3), 1800)]
    usage = WindowUsage(3600, 0, 2, oldest)

    when = earliest_headroom_at(
        closed,
        usage=usage,
        now=NOW,
        window_hours=5,
        ceiling_seconds=3600,
        reservation_seconds=1800,
    )

    assert when == oldest + timedelta(hours=5) + timedelta(seconds=1)


def test_open_reservations_alone_can_make_headroom_unreachable():
    """Nothing ages out of `reserved`, so a wake time would be a lie; the
    caller falls back to its own interval."""
    usage = WindowUsage(0, 3600, 1, None)
    assert (
        earliest_headroom_at(
            [],
            usage=usage,
            now=NOW,
            window_hours=5,
            ceiling_seconds=3600,
            reservation_seconds=1800,
        )
        is None
    )
