"""Typed attempt outcomes (SPEC §3.2): recorded from day one."""

from enum import StrEnum


class AttemptOutcome(StrEnum):
    CI_GREEN = "ci_green"
    CI_RED = "ci_red"
    AGENT_FAILURE = "agent_failure"
    INFRA_FAILURE = "infra_failure"
    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTH_FAILURE = "auth_failure"
    POLICY_BLOCK = "policy_block"
    TIMEOUT = "timeout"
    CANCELED = "canceled"


class DispatchBehavior(StrEnum):
    """SPEC §3.2: this spec ships only `retry`; `continuation` is a later second kind."""

    RETRY = "retry"


# SPEC §3.2: budget exhausted must mean N genuine failures, not N interruptions.
BUDGET_EXEMPT_OUTCOMES: frozenset[AttemptOutcome] = frozenset({AttemptOutcome.QUOTA_EXHAUSTED})
