"""Run state machine as a pure table (SPEC §3.2). Imports nothing."""

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    AWAITING_CI = "awaiting_ci"
    AWAITING_REVIEW = "awaiting_review"
    MERGING = "merging"
    BLOCKED_QUOTA = "blocked_quota"
    FAILED = "failed"
    PARKED = "parked"
    MERGED = "merged"
    CANCELED = "canceled"


TERMINAL_STATUSES: frozenset[RunStatus] = frozenset({RunStatus.MERGED, RunStatus.CANCELED})

_S = RunStatus

# SPEC §3.2, verbatim rows; canceled edges expanded from "any non-terminal".
TRANSITIONS: frozenset[tuple[RunStatus, RunStatus]] = frozenset(
    {
        (_S.QUEUED, _S.CLAIMED),  # dispatch: claim CAS + quota reservation, one txn
        (_S.QUEUED, _S.BLOCKED_QUOTA),  # provider exhausted / ceiling reached at dispatch
        (_S.QUEUED, _S.PARKED),  # PermanentError pre-attempt (bad config, repo 404)
        (_S.CLAIMED, _S.QUEUED),  # lease expired before container start
        (_S.CLAIMED, _S.RUNNING),  # container started
        (_S.CLAIMED, _S.FAILED),  # hard-deadline sweep
        (_S.RUNNING, _S.AWAITING_CI),  # attempt ended with PR, project oracle_gated
        (_S.RUNNING, _S.AWAITING_REVIEW),  # attempt ended with PR, project bootstrap
        (_S.RUNNING, _S.FAILED),  # attempt failure (incl. vanished container via lease)
        (_S.AWAITING_CI, _S.QUEUED),  # CI red, retry budget left -> fresh dispatch
        (_S.AWAITING_CI, _S.MERGING),  # CI green on up-to-date head
        (_S.AWAITING_CI, _S.FAILED),  # unrecoverable infra error while waiting (never timeout)
        (_S.AWAITING_CI, _S.PARKED),  # CI red budget spent, or ci_timeout
        (_S.AWAITING_REVIEW, _S.MERGING),  # operator accepts
        (_S.AWAITING_REVIEW, _S.PARKED),  # operator rejects (parked_reason='review_rejected')
        (_S.AWAITING_REVIEW, _S.FAILED),  # PR/repo gone out-of-band (infra only, no timeout)
        (_S.MERGING, _S.MERGED),  # squash-merge landed
        (_S.MERGING, _S.AWAITING_CI),  # base moved (oracle_gated); must re-earn green
        (_S.MERGING, _S.PARKED),  # merge conflict / merge blocked
        (_S.MERGING, _S.FAILED),  # unrecoverable infra error
        (_S.BLOCKED_QUOTA, _S.QUEUED),  # wake at exhausted_until / headroom — automatic
        (_S.FAILED, _S.QUEUED),  # retry with backoff (next_attempt_at)
        (_S.FAILED, _S.BLOCKED_QUOTA),  # provider exhausted
        (_S.FAILED, _S.PARKED),  # retry budget spent, or PermanentError
        (_S.PARKED, _S.QUEUED),  # human requeue
    }
    | {(s, _S.CANCELED) for s in RunStatus if s not in TERMINAL_STATUSES}
)


class ParkedReason(StrEnum):
    CI_RED = "ci_red"
    MERGE_CONFLICT = "merge_conflict"
    MERGE_BLOCKED = "merge_blocked"
    CI_TIMEOUT = "ci_timeout"
    AGENT_FAILURE = "agent_failure"
    INFRA_FAILURE = "infra_failure"
    PERMANENT_ERROR = "permanent_error"
    DEADLINE = "deadline"
    REVIEW_REJECTED = "review_rejected"
