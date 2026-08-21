"""The one definition of "how much of this account's window is spoken for".

SPEC §7's admission arithmetic and SPEC §9's *display* obligation are the same
arithmetic, so they are the same SQL: `api/routes.py`'s `/api/v1/quota`
endpoint and `quota/admission.py`'s ceiling check both read through this
module. The operator's headroom figure and the number that refuses a claim can
therefore never drift apart — which is the whole point of showing it.

`consumed` and `reserved` are disjoint buckets over the same rows, partitioned
on `actual_wallclock_s IS [NOT] NULL`, so nothing is counted twice. `consumed`
is windowed; `reserved` is not — an attempt that began outside the rolling
window and has not resolved is still an outstanding claim on capacity.

Every predicate takes an explicit `now` (plan decision D8). There is no clock
port: passing the parameter is what makes issue #24's synthetic-clock window
tests direct, and the endpoint simply passes its own `datetime.now(UTC)`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, DateTime, ScalarSelect, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from werft.db.models import ProviderAccount, QuotaLedgerEntry


def window_floor(now: datetime) -> ColumnElement[datetime]:
    # `make_interval`'s positional signature is (years, months, weeks, days,
    # hours, mins, secs) — the zeros put `rolling_window_hours` in the fifth
    # slot, per account rather than per query.
    return literal(now, DateTime(timezone=True)) - func.make_interval(
        0, 0, 0, 0, ProviderAccount.rolling_window_hours
    )


def consumed_subq(now: datetime) -> ScalarSelect[int]:
    return (
        select(func.coalesce(func.sum(QuotaLedgerEntry.actual_wallclock_s), 0))
        .where(QuotaLedgerEntry.provider_account_id == ProviderAccount.id)
        .where(QuotaLedgerEntry.actual_wallclock_s.is_not(None))
        .where(QuotaLedgerEntry.consumed_at >= window_floor(now))
        .correlate(ProviderAccount)
        .scalar_subquery()
    )


def reserved_subq() -> ScalarSelect[int]:
    """Age-independent on purpose (SPEC §7): an open reservation is capacity
    Werft has already promised, whatever its age."""
    return (
        select(func.coalesce(func.sum(QuotaLedgerEntry.reserved_wallclock_s), 0))
        .where(QuotaLedgerEntry.provider_account_id == ProviderAccount.id)
        .where(QuotaLedgerEntry.actual_wallclock_s.is_(None))
        .correlate(ProviderAccount)
        .scalar_subquery()
    )


def entry_count_subq(now: datetime) -> ScalarSelect[int]:
    """SPEC §7's run-count fallback (`window_cap_runs`): entries, not seconds."""
    return (
        select(func.count())
        .select_from(QuotaLedgerEntry)
        .where(QuotaLedgerEntry.provider_account_id == ProviderAccount.id)
        .where(QuotaLedgerEntry.consumed_at >= window_floor(now))
        .correlate(ProviderAccount)
        .scalar_subquery()
    )


def oldest_in_window_subq(now: datetime) -> ScalarSelect[datetime]:
    """`window_cap_runs`' wake time is when the oldest in-window entry leaves."""
    return (
        select(func.min(QuotaLedgerEntry.consumed_at))
        .where(QuotaLedgerEntry.provider_account_id == ProviderAccount.id)
        .where(QuotaLedgerEntry.consumed_at >= window_floor(now))
        .correlate(ProviderAccount)
        .scalar_subquery()
    )


@dataclass(frozen=True)
class WindowUsage:
    consumed_seconds: int
    reserved_seconds: int
    entry_count: int
    oldest_in_window_at: datetime | None


@dataclass(frozen=True)
class ClosedEntry:
    consumed_at: datetime
    actual_seconds: int


async def read_window(session: AsyncSession, account_id: UUID, *, now: datetime) -> WindowUsage:
    row = (
        await session.execute(
            select(
                consumed_subq(now).label("consumed"),
                reserved_subq().label("reserved"),
                entry_count_subq(now).label("entries"),
                oldest_in_window_subq(now).label("oldest"),
            )
            .select_from(ProviderAccount)
            .where(ProviderAccount.id == account_id)
        )
    ).one()
    return WindowUsage(int(row.consumed), int(row.reserved), int(row.entries), row.oldest)


async def read_closed_in_window(
    session: AsyncSession, account_id: UUID, *, now: datetime, window_hours: int
) -> list[ClosedEntry]:
    """The rows that can still age out, oldest first. Open reservations are
    excluded: they leave the window without freeing anything."""
    floor = now - timedelta(hours=window_hours)
    rows = (
        await session.execute(
            select(QuotaLedgerEntry.consumed_at, QuotaLedgerEntry.actual_wallclock_s)
            .where(
                QuotaLedgerEntry.provider_account_id == account_id,
                QuotaLedgerEntry.actual_wallclock_s.is_not(None),
                QuotaLedgerEntry.consumed_at >= floor,
            )
            .order_by(QuotaLedgerEntry.consumed_at, QuotaLedgerEntry.id)
        )
    ).all()
    return [ClosedEntry(consumed_at=c, actual_seconds=int(a)) for c, a in rows]


def earliest_headroom_at(
    closed: Sequence[ClosedEntry],
    *,
    usage: WindowUsage,
    now: datetime,
    window_hours: int,
    ceiling_seconds: int,
    reservation_seconds: int,
) -> datetime | None:
    """When `reservation_seconds` will next fit under the ceiling, or `None`.

    Closed in-window entries are dropped in `consumed_at` order — each one
    leaves the window `window_hours` after it was consumed — until the
    remaining load plus the reservation fits. `None` means ageing alone can
    never help (open reservations, or the reservation itself, exceed the
    ceiling) and the caller falls back to a fixed interval.

    One function, two callers (plan decision D9): the `queued ->
    blocked_quota` edge's `next_attempt_at` and `advance_failed`'s
    `blocked_quota` wake. Two definitions could disagree; one cannot.
    """
    remaining = usage.consumed_seconds + usage.reserved_seconds
    if remaining + reservation_seconds <= ceiling_seconds:
        return now
    window = timedelta(hours=window_hours)
    for entry in sorted(closed, key=lambda e: e.consumed_at):
        remaining -= entry.actual_seconds
        if remaining + reservation_seconds <= ceiling_seconds:
            # One second past the boundary: the membership predicate is
            # `consumed_at >= floor`, so *at* `consumed_at + window` the row is
            # still counted.
            return max(entry.consumed_at + window + timedelta(seconds=1), now)
    return None
