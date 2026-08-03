"""Attempt finalization: the seam between a finished attempt (a provider
adapter's `Classification`) and the run's next legal state (SPEC §3.2; plan
Behavioral decisions 8 and 9).

Two crash-window guarantees this module owns, load-bearing rather than
incidental:

- **PR-first-then-CAS** (`open_pr_and_wait`): `RepoOps.open_pr` is called
  before the `running -> awaiting_ci`/`awaiting_review` CAS. A crash between
  the two leaves GitHub holding the PR but the run row still `running`; a
  re-driven caller's second `open_pr` call lands on GitHub's own
  adopt-on-422 path (same PR, same number) and the CAS that follows either
  applies for the first time or is legitimately stale (a prior, uncrashed
  call already applied it) — either way this function returns cleanly
  rather than raising.
- **Two-step CAS out of `running`** (`finalize_attempt`): SPEC §3.2 has no
  direct `running -> blocked_quota` or `running -> parked` edge, so a
  quota-exhausted or budget-spent attempt always lands on `running ->
  failed` first; `advance_failed` immediately re-reads the fresh row and
  moves it on from there. Both CAS calls run inside the same
  session/transaction the caller commits — no partially-advanced state is
  ever visible past a commit.
- **`running -> canceled` races `running -> failed` (`finalize_attempt`)**:
  `running -> canceled` is a legal, routine edge (SPEC §3.2: any
  non-terminal status can cancel) — an operator can cancel a run while its
  container is still finishing. When that race loses the `running ->
  failed` CAS, the attempt row is already closed and quota already released
  (both run unconditionally, above the branch); finalizing must return
  cleanly rather than raise, or the caller's transaction unwinds *both* of
  those and leaks the just-released quota back out as headroom nobody
  reserved. Only a version mismatch where the run is still (impossibly)
  `running` is a genuine bug worth raising for.

`finalize_attempt` also calls `quota.release` in that same transaction,
*before* branching on success/failure: releasing an attempt's reserved
quota is a property of the attempt being over, not of how it ended.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from werft.contracts.result import ResultStatus
from werft.db.models import BacklogItem, Project, Run, RunAttempt
from werft.db.transitions import transition_run
from werft.domain.attempts import BUDGET_EXEMPT_OUTCOMES, AttemptOutcome
from werft.domain.projects import ProjectLifecycle
from werft.domain.runs import ParkedReason, RunStatus, run_branch_name
from werft.github.ops import RepoOps
from werft.observe.alerts import AlertSink
from werft.providers.base import Classification


class QuotaPort(Protocol):
    """The quota-release seam `finalize_attempt` calls inside its own
    transaction. T7 replaces `NullQuota` with the real ledger true-up
    (`quota_ledger.actual_wallclock_s`); the seam exists now so
    `finalize_attempt` never has to change shape to grow it."""

    async def release(
        self, session: AsyncSession, run: Run, observed_seconds: int | None
    ) -> None: ...


class NullQuota:
    """No-op `QuotaPort` — T5 ships no quota ledger writer yet (T7
    territory); this keeps `finalize_attempt` callable and tested today."""

    async def release(self, session: AsyncSession, run: Run, observed_seconds: int | None) -> None:
        return None


async def _current_attempt(session: AsyncSession, run_id: UUID) -> RunAttempt:
    """The run's open attempt row (`ended_at IS NULL`). Dispatch (T7) seeds
    exactly one per attempt before it starts; finalizing is what closes it
    out."""
    result = await session.execute(
        select(RunAttempt)
        .where(RunAttempt.run_id == run_id, RunAttempt.ended_at.is_(None))
        .order_by(RunAttempt.attempt_no.desc())
        .limit(1)
    )
    return result.scalars().one()


async def finalize_attempt(
    session: AsyncSession,
    ops: RepoOps,
    run: Run,
    project: Project,
    *,
    classification: Classification,
    pushed: bool,
    quota: QuotaPort,
    alerts: AlertSink,
) -> None:
    """Close out `run`'s open attempt row and move it off `running`.

    Fills `outcome`/`ended_at`/`duration_seconds` on the latest open
    `run_attempts` row. On a *pushed* `SUCCESS`, `outcome` stays `None` —
    doctrine #1: only an executed check (the oracle) decides whether work is
    good, so a clean-exit attempt with a PR in flight is not a verdict yet; a
    later CI-watch task fills `ci_green`/`ci_red` once the oracle reports. A
    `SUCCESS` that pushed nothing never reaches an oracle at all — doctrine
    #1's NULL encoding does not apply, so it is recharacterized as a concrete
    `agent_failure` outcome (detail `"success without push"`) before this
    function does anything else, and rides the same ladder as any other
    genuine failure.

    `quota.release` runs in this same transaction, before the branch below:
    releasing reserved quota is a property of the attempt ending, not of how
    it ended. Then: a pushed success moves on to `open_pr_and_wait`;
    everything else — including a clean success that pushed nothing — takes
    the two-step `running -> failed -> {blocked_quota,queued,parked}` path,
    since SPEC §3.2 has no direct edge out of `running` for those. An
    `auth_failure` outcome additionally fires `alerts.auth_failure`, and a
    run that lands `parked` fires `alerts.run_parked` — both read off the
    post-`advance_failed` row rather than being threaded through it, so
    `advance_failed` itself stays alert-free (SPEC layering: it needs no
    `AlertSink` to do its job).

    A lost `running -> failed` CAS is not always a bug: `running ->
    canceled` is itself a legal edge, so an operator cancel racing this
    finalize is routine, not exceptional. On CAS loss this re-reads the run;
    if it has left `running` (canceled or otherwise already advanced by
    another caller), the attempt row is already closed and quota already
    released above, so this returns cleanly instead of raising and unwinding
    that release. A still-`running` row with a mismatched version is the
    only case worth raising for.
    """
    pushed_success = classification.status == ResultStatus.SUCCESS and pushed
    if classification.status == ResultStatus.SUCCESS and not pushed:
        classification = replace(
            classification,
            outcome=AttemptOutcome.AGENT_FAILURE,
            detail="success without push",
        )

    attempt = await _current_attempt(session, run.id)
    ended_at = datetime.now(UTC)
    duration = max(0, int((ended_at - attempt.started_at).total_seconds()))
    await session.execute(
        update(RunAttempt)
        .where(RunAttempt.id == attempt.id)
        .values(
            outcome=classification.outcome.value if classification.outcome else None,
            ended_at=ended_at,
            duration_seconds=duration,
        )
    )
    await quota.release(session, run, duration)

    if pushed_success:
        await open_pr_and_wait(session, ops, run, project, alerts=alerts)
        return

    ok = await transition_run(
        session,
        run_id=run.id,
        expected_version=run.version,
        new_status=RunStatus.FAILED,
        extra={"error_message": classification.detail},
    )
    if not ok:
        current = await session.get(Run, run.id, populate_existing=True)
        if current.status != RunStatus.RUNNING.value:
            return  # legitimate race (e.g. canceled) — nothing left to advance
        raise RuntimeError(f"finalize_attempt: lost CAS race on run {run.id} (running->failed)")

    failed_run = await session.get(Run, run.id, populate_existing=True)
    await advance_failed(
        session,
        failed_run,
        outcome=classification.outcome,
        exhausted_until=classification.exhausted_until,
        quota=quota,
    )

    if classification.outcome == AttemptOutcome.AUTH_FAILURE:
        await alerts.auth_failure(failed_run.provider or "unknown")

    advanced = await session.get(Run, run.id, populate_existing=True)
    if advanced.status == RunStatus.PARKED:
        await alerts.run_parked(project.slug, run.id, advanced.parked_reason or "unknown")


async def open_pr_and_wait(
    session: AsyncSession,
    ops: RepoOps,
    run: Run,
    project: Project,
    *,
    alerts: AlertSink,
) -> None:
    """Open (or adopt) `run`'s PR, then CAS `running` onward per lifecycle.

    PR-first-then-CAS (module docstring): a crash between the two leaves the
    PR live on GitHub but the run row still `running`. A re-driven call's
    `ops.open_pr` lands on the adopt-on-422 path and returns the same PR;
    the CAS that follows either applies for the first time, or — if a
    prior, uncrashed call already applied it — loses the race on a stale
    version. That second case is re-read and checked: the same target
    status with the same `pr_number` means this call is a harmless
    duplicate, not an error.
    """
    item = await session.get(BacklogItem, run.backlog_item_id)
    head = run_branch_name(run.id)
    title = f"werft: {item.title} (#{item.github_issue_number})"
    # A plain reference, deliberately *not* a `Closes #N` keyword: GitHub
    # auto-closes a linked issue only when the PR merges into the
    # repository's **default** branch, and every run PR targets
    # `project.unattended_branch` instead (SPEC §6.1). The keyword would
    # therefore be inert here — a promise the topology cannot keep, which
    # reads to an operator as "Werft closes my issues" while it silently
    # never does. Retiring the backlog item is `merge_flow._land_merged`'s
    # job (`is_eligible=False` + `remove_label`), not GitHub's.
    body = f"Issue: #{item.github_issue_number}\n\nWerft run: {run.id}"
    pr = await ops.open_pr(head, project.unattended_branch, title, body)

    is_bootstrap = project.lifecycle == ProjectLifecycle.BOOTSTRAP
    target = RunStatus.AWAITING_REVIEW if is_bootstrap else RunStatus.AWAITING_CI
    ok = await transition_run(
        session,
        run_id=run.id,
        expected_version=run.version,
        new_status=target,
        extra={"pr_number": pr.number},
    )
    if not ok:
        current = await session.get(Run, run.id, populate_existing=True)
        if current.status == target.value and current.pr_number == pr.number:
            return  # a prior, uncrashed call already advanced this — re-drive, not a race
        raise RuntimeError(
            f"open_pr_and_wait: lost CAS race on run {run.id} (running->{target.value})"
        )

    if is_bootstrap:
        await alerts.review_waiting(project.slug, run.id, pr.html_url)


async def advance_failed(
    session: AsyncSession,
    run: Run,
    *,
    outcome: AttemptOutcome | None,
    exhausted_until: datetime | None,
    quota: QuotaPort,
) -> None:
    """Behavioral decision 8, verbatim (thin plan): route a `failed` run on.

    `quota` is accepted but unused today — a deliberately reserved seam so
    T7's window-headroom refinement of the `blocked_quota` wake time can
    extend this function without changing its signature or its callers.

    Takes `outcome` and `exhausted_until` directly rather than the whole
    `Classification` `finalize_attempt` holds: those are the two values this
    ladder actually branches on, and `failed` is reachable from more callers
    than just `finalize_attempt`'s `running -> failed` — SPEC §3.2 also gives
    `claimed`, `awaiting_ci`, `awaiting_review`, and `merging` a `-> failed`
    edge, none of which have any provider `Classification` to hand this
    function at all. `exhausted_until` is *this attempt's* provider-reported
    reset time carried on `Classification`; it is not the same thing as
    `provider_accounts.exhausted_until` (that column is the account-level
    durable record T7's quota ledger owns) — it has no durable column of its
    own to be re-read from, so it must be threaded through by whichever
    caller already holds it.

    Every non-exempt outcome — `agent_failure`, `infra_failure`,
    `auth_failure`, `policy_block`, `timeout`, `canceled` — shares the same
    ladder and, if it parks, the one generic `parked_reason='agent_failure'`:
    provider-specific parked reasons are post-milestone fallthrough (the
    `parked_reason` CHECK constraint has no dedicated slot for the others
    yet).
    """
    if outcome in BUDGET_EXEMPT_OUTCOMES:
        next_attempt_at = exhausted_until or (datetime.now(UTC) + timedelta(minutes=15))
        ok = await transition_run(
            session,
            run_id=run.id,
            expected_version=run.version,
            new_status=RunStatus.BLOCKED_QUOTA,
            extra={"next_attempt_at": next_attempt_at},
        )
        if not ok:
            raise RuntimeError(
                f"advance_failed: lost CAS race on run {run.id} (failed->blocked_quota)"
            )
        return

    attempt_count = run.attempt_count + 1
    if attempt_count >= run.max_attempts:
        target = RunStatus.PARKED
        extra = {
            "attempt_count": attempt_count,
            "parked_reason": ParkedReason.AGENT_FAILURE.value,
        }
    else:
        backoff_seconds = min(2**attempt_count * 30, 1800)
        target = RunStatus.QUEUED
        extra = {
            "attempt_count": attempt_count,
            "next_attempt_at": datetime.now(UTC) + timedelta(seconds=backoff_seconds),
        }
    ok = await transition_run(
        session, run_id=run.id, expected_version=run.version, new_status=target, extra=extra
    )
    if not ok:
        raise RuntimeError(
            f"advance_failed: lost CAS race on run {run.id} (failed->{target.value})"
        )
