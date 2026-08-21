from datetime import UTC, datetime, timedelta

from werft.quota.admission import AccountLimits, decide
from werft.quota.window import WindowUsage

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def limits(**over) -> AccountLimits:
    base = dict(
        ceiling_seconds=18000,
        rolling_window_hours=5,
        window_cap_runs=None,
        provider_window_capacity_seconds=None,
        exhausted_until=None,
        last_reading_utilization=None,
        last_reading_at=None,
        is_active=True,
    )
    return AccountLimits(**(base | over))


def call(lim, usage, *, reservation=5400, closed=(), now=NOW):
    return decide(
        lim, usage, closed, reservation_seconds=reservation, now=now, fallback_seconds=900
    )


def test_admits_when_consumed_plus_reserved_plus_reservation_fits():
    got = call(limits(), WindowUsage(7000, 5400, 2, NOW))
    assert (got.ok, got.reason) == (True, "ok")


def test_refuses_the_claim_that_would_cross_the_ceiling():
    got = call(limits(), WindowUsage(7000, 5400, 2, NOW), reservation=5601)
    assert (got.ok, got.reason) == (False, "ceiling")
    assert got.retry_at is not None


def test_an_inactive_account_admits_nothing():
    got = call(limits(is_active=False), WindowUsage(0, 0, 0, None), reservation=1)
    assert got.reason == "inactive"


def test_exhausted_until_in_the_future_beats_an_empty_ledger():
    """SPEC §7: a parsed provider refusal always beats the ledger's optimism,
    and its wake time IS `exhausted_until` (#24 acceptance 3)."""
    until = NOW + timedelta(minutes=30)
    got = call(limits(exhausted_until=until), WindowUsage(0, 0, 0, None))
    assert (got.ok, got.reason, got.retry_at) == (False, "exhausted_until", until)


def test_exhausted_until_in_the_past_no_longer_blocks():
    got = call(limits(exhausted_until=NOW - timedelta(seconds=1)), WindowUsage(0, 0, 0, None))
    assert got.ok is True


def test_a_convertible_reading_above_the_ledger_tightens():
    got = call(
        limits(
            provider_window_capacity_seconds=18000,
            last_reading_utilization=90.0,
            last_reading_at=NOW - timedelta(minutes=1),
        ),
        WindowUsage(1000, 0, 1, NOW),
    )
    assert got.effective_consumed_seconds == 16200
    assert (got.ok, got.reason) == (False, "ceiling")


def test_a_reading_below_the_ledger_never_increases_headroom():
    """#24 acceptance 4, verbatim. `max()` — never assignment, never `min()`:
    a reading below the ledger is a reading that knows less than we do, and
    letting it lower `consumed` would hand back headroom nobody released."""
    got = call(
        limits(
            provider_window_capacity_seconds=18000,
            last_reading_utilization=1.0,
            last_reading_at=NOW - timedelta(minutes=1),
        ),
        WindowUsage(17000, 0, 3, NOW),
    )
    assert got.effective_consumed_seconds == 17000
    assert got.ok is False


def test_a_stale_reading_is_ignored_entirely():
    """SPEC §7: "readings older than 15 min are ignored"."""
    got = call(
        limits(
            provider_window_capacity_seconds=18000,
            last_reading_utilization=99.0,
            last_reading_at=NOW - timedelta(minutes=16),
        ),
        WindowUsage(0, 0, 0, None),
    )
    assert (got.ok, got.effective_consumed_seconds) == (True, 0)


def test_an_unconvertible_reading_at_or_above_95_percent_blocks_outright():
    reading_at = NOW - timedelta(minutes=1)
    got = call(
        limits(last_reading_utilization=95.0, last_reading_at=reading_at),
        WindowUsage(0, 0, 0, None),
    )
    assert (got.ok, got.reason) == (False, "provider_reading")
    assert got.retry_at == reading_at + timedelta(minutes=15)


def test_an_unconvertible_reading_below_95_percent_is_display_only():
    got = call(
        limits(last_reading_utilization=94.9, last_reading_at=NOW - timedelta(minutes=1)),
        WindowUsage(0, 0, 0, None),
    )
    assert got.ok is True


def test_window_cap_runs_is_the_run_count_fallback():
    oldest = NOW - timedelta(hours=1)
    blocked = call(limits(window_cap_runs=3), WindowUsage(0, 0, 3, oldest), reservation=60)
    assert blocked.reason == "window_cap"
    assert blocked.retry_at == oldest + timedelta(hours=5)
    assert call(limits(window_cap_runs=3), WindowUsage(0, 0, 2, oldest), reservation=60).ok is True


def test_the_refusal_order_is_exhaustion_then_reading_then_cap_then_ceiling():
    """All four true at once: the answer must be the earliest rule, because
    that is the one whose wake time is honest."""
    got = call(
        limits(
            exhausted_until=NOW + timedelta(hours=1),
            window_cap_runs=1,
            last_reading_utilization=99.0,
            last_reading_at=NOW,
        ),
        WindowUsage(18000, 0, 9, NOW - timedelta(hours=1)),
    )
    assert got.reason == "exhausted_until"
