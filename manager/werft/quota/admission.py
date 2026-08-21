"""SPEC §7's admission rule as one pure function.

No session, no clock, no I/O: everything it needs is passed in, so the whole
rule — including the parts that only fire on a provider reading nobody can
reproduce on demand — is exercised by unit tests with literals.

Two asymmetries are load-bearing and deliberately conservative (SPEC §7): a
provider signal wins **only when it tightens**, and a reading older than 15
minutes is ignored rather than trusted. The first is `max()`, not assignment;
that single call is issue #24's fourth acceptance criterion, made structural
instead of remembered.

The refusal order matters as much as the rules. Each refusal carries the wake
time that *its own* rule implies, so `queued -> blocked_quota` never sleeps on
a reason that is not the binding one.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from werft.quota.window import ClosedEntry, WindowUsage, earliest_headroom_at

#: SPEC §7: "readings older than 15 min are ignored".
PROVIDER_READING_MAX_AGE = timedelta(minutes=15)
#: SPEC §7: a non-convertible reading at or above this blocks dispatch outright.
PROVIDER_BLOCK_UTILIZATION = 95.0


@dataclass(frozen=True)
class AccountLimits:
    """`provider_accounts`, as admission sees it — plain values, no ORM row."""

    ceiling_seconds: int
    rolling_window_hours: int
    window_cap_runs: int | None
    provider_window_capacity_seconds: int | None
    exhausted_until: datetime | None
    last_reading_utilization: float | None
    last_reading_at: datetime | None
    is_active: bool


@dataclass(frozen=True)
class Admission:
    ok: bool
    reason: str
    effective_consumed_seconds: int
    retry_at: datetime | None = None


def decide(
    limits: AccountLimits,
    usage: WindowUsage,
    closed_in_window: Sequence[ClosedEntry],
    *,
    reservation_seconds: int,
    now: datetime,
    fallback_seconds: int,
) -> Admission:
    consumed = usage.consumed_seconds
    window = timedelta(hours=limits.rolling_window_hours)
    fallback = now + timedelta(seconds=fallback_seconds)

    if not limits.is_active:
        return Admission(False, "inactive", consumed, fallback)

    # 1. A parsed provider refusal always beats the ledger's optimism, and its
    #    wake time IS the reported reset (#24 acceptance 3).
    if limits.exhausted_until is not None and limits.exhausted_until > now:
        return Admission(False, "exhausted_until", consumed, limits.exhausted_until)

    # 2. A fresh provider reading, and only ever in the tightening direction.
    fresh = (
        limits.last_reading_utilization is not None
        and limits.last_reading_at is not None
        and limits.last_reading_at >= now - PROVIDER_READING_MAX_AGE
    )
    if fresh:
        capacity = limits.provider_window_capacity_seconds
        utilization = float(limits.last_reading_utilization)
        if capacity is not None:
            # Round UP: truncating a fractional derived value would
            # under-estimate consumed and loosen admission, violating the
            # "provider reading never loosens" guarantee (#24 acceptance 4).
            consumed = max(consumed, math.ceil(utilization / 100.0 * capacity))
        elif utilization >= PROVIDER_BLOCK_UTILIZATION:
            # Not convertible to window-seconds, but too close to the wall to
            # dispatch on. It stops binding when the reading goes stale.
            return Admission(
                False,
                "provider_reading",
                consumed,
                limits.last_reading_at + PROVIDER_READING_MAX_AGE,
            )

    # 3. The run-count fallback.
    if limits.window_cap_runs is not None and usage.entry_count >= limits.window_cap_runs:
        retry_at = (
            usage.oldest_in_window_at + window
            if usage.oldest_in_window_at is not None
            else fallback
        )
        return Admission(False, "window_cap", consumed, max(retry_at, now))

    # 4. The ceiling itself.
    if consumed + usage.reserved_seconds + reservation_seconds > limits.ceiling_seconds:
        retry_at = earliest_headroom_at(
            closed_in_window,
            usage=usage,
            now=now,
            window_hours=limits.rolling_window_hours,
            ceiling_seconds=limits.ceiling_seconds,
            reservation_seconds=reservation_seconds,
        )
        return Admission(False, "ceiling", consumed, retry_at or fallback)

    return Admission(True, "ok", consumed, None)
