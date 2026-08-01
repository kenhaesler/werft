from hypothesis import given
from hypothesis import strategies as st

from werft.domain.attempts import BUDGET_EXEMPT_OUTCOMES, AttemptOutcome
from werft.domain.runs import TERMINAL_STATUSES, TRANSITIONS, RunStatus

ALL = list(RunStatus)


def test_eleven_states_and_terminal_set() -> None:
    assert len(ALL) == 11
    assert {RunStatus.MERGED, RunStatus.CANCELED} == TERMINAL_STATUSES


def test_exact_edge_count() -> None:
    # 25 named edges + 9 "any non-terminal -> canceled" (SPEC §3.2)
    assert len(TRANSITIONS) == 34


def test_spot_edges_from_spec() -> None:
    s = RunStatus
    for edge in [
        (s.QUEUED, s.CLAIMED),
        (s.RUNNING, s.AWAITING_REVIEW),
        (s.AWAITING_REVIEW, s.MERGING),
        (s.AWAITING_REVIEW, s.PARKED),
        (s.MERGING, s.AWAITING_CI),
        (s.BLOCKED_QUOTA, s.QUEUED),
        (s.FAILED, s.BLOCKED_QUOTA),
        (s.PARKED, s.QUEUED),
    ]:
        assert edge in TRANSITIONS
    for non_edge in [
        (s.QUEUED, s.RUNNING),  # must pass through claimed
        (s.AWAITING_REVIEW, s.QUEUED),  # review never silently requeues
        (s.MERGED, s.QUEUED),
        (s.CANCELED, s.QUEUED),
    ]:
        assert non_edge not in TRANSITIONS


@given(st.sampled_from(ALL), st.sampled_from(ALL))
def test_terminal_states_have_no_outgoing(frm: RunStatus, to: RunStatus) -> None:
    if frm in TERMINAL_STATUSES:
        assert (frm, to) not in TRANSITIONS


@given(st.sampled_from(ALL))
def test_no_self_loops(s: RunStatus) -> None:
    assert (s, s) not in TRANSITIONS


@given(st.sampled_from(ALL))
def test_every_non_terminal_admits_cancel(s: RunStatus) -> None:
    if s not in TERMINAL_STATUSES:
        assert (s, RunStatus.CANCELED) in TRANSITIONS


def test_parked_is_never_terminal() -> None:
    assert (RunStatus.PARKED, RunStatus.QUEUED) in TRANSITIONS


@given(st.sampled_from(ALL))
def test_every_state_reaches_terminal(start: RunStatus) -> None:
    seen, frontier = {start}, [start]
    while frontier:
        cur = frontier.pop()
        for frm, to in TRANSITIONS:
            if frm == cur and to not in seen:
                seen.add(to)
                frontier.append(to)
    assert seen & TERMINAL_STATUSES


def test_quota_exhausted_is_distinct_and_budget_exempt() -> None:
    # SPEC §3.2: distinct from ci_red and agent_failure, never consumes retry budget
    assert AttemptOutcome.QUOTA_EXHAUSTED not in (
        AttemptOutcome.CI_RED,
        AttemptOutcome.AGENT_FAILURE,
    )
    assert {AttemptOutcome.QUOTA_EXHAUSTED} == BUDGET_EXEMPT_OUTCOMES
