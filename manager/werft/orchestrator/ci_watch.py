"""CI watch + the bootstrap flip: the `awaiting_ci` wait (SPEC §3.2 edges;
SPEC §6.2 mechanics — the `ci_timeout` bound, poll-only, no inline sleeps)
and the automatic doctrine-#1 lifecycle flip (SPEC §3.1; SPEC §6.3 the
protection ordering it completes) on the first green `werft-oracle` check.

Three functions, one shared idempotency seam:

- **`advance_awaiting_ci`**: one poll's worth of decision for one
  `awaiting_ci` run. A gone PR (closed, not merged, or 404 — out-of-band,
  never a timeout: SPEC §3.2) CASes straight to `failed`. A decisive oracle
  read (`SUCCESS`/`FAILURE`) fills the still-open `run_attempts` row's
  `outcome` (plan Behavioral decision 3: the attempt was left `NULL` —
  "pending oracle" — by `finalize.py`'s pushed-success path; the oracle's
  verdict is what finally closes it) and writes one `run_events`
  `ci_observed` row, *only* on that decisive read — a `PENDING`/`ABSENT`
  poll every 30 s would otherwise spam the table with nothing new to say.
  `PENDING`/`ABSENT` instead check the `ci_timeout` bound: decision 2 fixes
  the wait's epoch as the `created_at` of the latest `status_changed`
  `run_events` row landing on `awaiting_ci` (the transition trigger writes
  it; no new column), so both a first wait and a `merging -> awaiting_ci`
  re-entry (base moved, a later task) restart the clock for free.
- **`flip_project`**: the shared, idempotent flip both this module's
  `check_flip` and B3's manual-flip API consume. A guarded `UPDATE
  projects SET lifecycle=:to WHERE id=:id AND lifecycle=:from` is the whole
  idempotency story — rowcount 0 (already flipped, or never was in the
  expected `from` state) means zero side effects, not an error: the
  branch-protection call, `project_events` rows, and `alerts.project_flipped`
  all sit *after* the guard and only run when it actually won. A second,
  otherwise-identical call converges to a no-op rather than re-applying
  protection or re-alerting.
- **`check_flip`**: the observation site (plan decision 4) — scans a
  bootstrap project's `awaiting_review` runs (the workflow this flip gates
  on lands via the PR that creates it, and runs on that same PR's head) for
  a green oracle and, on green, flips via `flip_project`. The observed run
  itself is never touched: the flip is a project-level fact, and the run
  stays in the operator's review queue exactly as before.

`GitHubUnavailable` (transient: rate limit, 5xx, transport) is caught around
every *pre-mutation* read (`get_pr`, `oracle_check`) in both
`advance_awaiting_ci` and `check_flip` — logged and swallowed, state
untouched, so the next tick simply retries. It is deliberately **not**
caught around `flip_project`'s own GitHub calls: those run only after this
transaction's guarded `UPDATE` has already applied, so letting any error
there propagate is what lets the caller's transaction roll back instead of
committing a lifecycle flip GitHub never actually protected. Any other
`GitHubApiError` is likewise left to escape everywhere — the orchestrator
loop (a later task) isolates per-run errors; these handlers isolate only
the noisy, expected-to-be-transient case.

Handlers are short-lived and re-drivable (SPEC §3.3.4): a lost CAS out of
`awaiting_ci` is checked against a fresh read before deciding whether to
raise — a genuine race (an operator's concurrent cancel is the one legal
`awaiting_ci -> canceled` edge) means the row has already moved on and this
call has nothing left to do; a version mismatch while the row is still
(impossibly, under the single-writer design) `awaiting_ci` is a real bug.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from werft.db.models import Project, ProjectEvent, Run, RunAttempt, RunEvent
from werft.db.transitions import transition_run
from werft.domain.attempts import AttemptOutcome
from werft.domain.projects import ProjectLifecycle
from werft.domain.runs import ParkedReason, RunStatus
from werft.github.client import GitHubUnavailable
from werft.github.ops import CheckState, RepoOps
from werft.observe.alerts import AlertSink

logger = structlog.get_logger(__name__)


async def _latest_attempt(session: AsyncSession, run_id: UUID) -> RunAttempt:
    """The highest-`attempt_no` `run_attempts` row for `run_id`. Unlike
    `finalize.py`'s `_current_attempt` (which filters on `ended_at IS
    NULL`), this attempt is already closed out — `finalize_attempt`'s
    pushed-success path set `ended_at`/`duration_seconds` and left
    `outcome` `NULL` ("pending oracle"); this module is what finally fills
    that column in."""
    result = await session.execute(
        select(RunAttempt)
        .where(RunAttempt.run_id == run_id)
        .order_by(RunAttempt.attempt_no.desc())
        .limit(1)
    )
    return result.scalars().one()


async def _fill_attempt_outcome(
    session: AsyncSession, run_id: UUID, outcome: AttemptOutcome
) -> None:
    attempt = await _latest_attempt(session, run_id)
    await session.execute(
        update(RunAttempt).where(RunAttempt.id == attempt.id).values(outcome=outcome.value)
    )


def _record_ci_observed(session: AsyncSession, run_id: UUID, *, sha: str, conclusion: str) -> None:
    """Decisive-only: called for `SUCCESS`/`FAILURE`, never `PENDING`/`ABSENT`
    (a 30 s poller writing a row on every no-verdict tick would spam
    `run_events` with nothing new to say)."""
    session.add(
        RunEvent(
            run_id=run_id,
            event_type="ci_observed",
            payload={"sha": sha, "conclusion": conclusion},
        )
    )


async def _awaiting_ci_since(session: AsyncSession, run_id: UUID) -> datetime | None:
    """Plan Behavioral decision 2: the `ci_timeout` wait's epoch is the
    `created_at` of the latest `status_changed` `run_events` row whose
    payload landed the run on `awaiting_ci` — written by the transition
    trigger itself (`0001_spine.py`'s `runs_enforce_transition`), so no new
    column is needed and a later `merging -> awaiting_ci` re-entry restarts
    the clock for free."""
    result = await session.execute(
        text(
            "SELECT created_at FROM run_events "
            "WHERE run_id = :run_id AND event_type = 'status_changed' "
            "AND payload ->> 'to' = 'awaiting_ci' "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"run_id": run_id},
    )
    return result.scalar_one_or_none()


async def _reject_lost_cas_unless_raced(session: AsyncSession, run: Run, edge: str) -> None:
    """A lost CAS out of `awaiting_ci` is only ever a race with a concurrent,
    already-completed advance (e.g. an operator's `awaiting_ci -> canceled`
    cancel) — re-reading and finding the row genuinely elsewhere means that
    other call's outcome stands and there is nothing left for this call to
    do. Still `awaiting_ci` despite the lost CAS is a version mismatch that
    should be impossible under the single-writer design, and is a bug worth
    raising for."""
    current = await session.get(Run, run.id, populate_existing=True)
    if current.status != RunStatus.AWAITING_CI.value:
        return
    raise RuntimeError(f"ci_watch: lost CAS race on run {run.id} ({edge})")


async def advance_awaiting_ci(
    session: AsyncSession, ops: RepoOps, run: Run, project: Project, *, alerts: AlertSink
) -> None:
    """One poll's worth of decision for one `awaiting_ci` run (plan
    Behavioral decisions 2-3; SPEC §3.2 edges). See the module docstring
    for the full decision table and the `GitHubUnavailable` containment
    boundary.
    """
    try:
        pr = await ops.get_pr(run.pr_number)
    except GitHubUnavailable as exc:
        logger.warning("ci_watch.get_pr_unavailable", run_id=str(run.id), error=str(exc))
        return

    if pr is None or (pr.state == "closed" and not pr.merged):
        ok = await transition_run(
            session,
            run_id=run.id,
            expected_version=run.version,
            new_status=RunStatus.FAILED,
            extra={"error_message": "PR gone out-of-band while awaiting CI"},
        )
        if not ok:
            await _reject_lost_cas_unless_raced(session, run, "awaiting_ci->failed")
        return

    try:
        state = await ops.oracle_check(pr.head_sha)
    except GitHubUnavailable as exc:
        logger.warning("ci_watch.oracle_check_unavailable", run_id=str(run.id), error=str(exc))
        return

    if state == CheckState.SUCCESS:
        await _fill_attempt_outcome(session, run.id, AttemptOutcome.CI_GREEN)
        _record_ci_observed(session, run.id, sha=pr.head_sha, conclusion=state.value)
        ok = await transition_run(
            session,
            run_id=run.id,
            expected_version=run.version,
            new_status=RunStatus.MERGING,
        )
        if not ok:
            await _reject_lost_cas_unless_raced(session, run, "awaiting_ci->merging")
        return

    if state == CheckState.FAILURE:
        await _fill_attempt_outcome(session, run.id, AttemptOutcome.CI_RED)
        _record_ci_observed(session, run.id, sha=pr.head_sha, conclusion=state.value)
        attempt_count = run.attempt_count + 1
        if attempt_count < run.max_attempts:
            target = RunStatus.QUEUED
            extra = {"attempt_count": attempt_count}
        else:
            target = RunStatus.PARKED
            extra = {"attempt_count": attempt_count, "parked_reason": ParkedReason.CI_RED.value}
        ok = await transition_run(
            session, run_id=run.id, expected_version=run.version, new_status=target, extra=extra
        )
        if not ok:
            await _reject_lost_cas_unless_raced(session, run, f"awaiting_ci->{target.value}")
            return
        if target == RunStatus.PARKED:
            await alerts.run_parked(project.slug, run.id, ParkedReason.CI_RED.value)
        return

    # PENDING or ABSENT: not a verdict yet (RepoOps.oracle_check's own
    # doctrine-#1 contract) — the only question left is whether the wait
    # has outrun the project's ci_timeout.
    since = await _awaiting_ci_since(session, run.id)
    if since is None or datetime.now(UTC) - since < timedelta(seconds=project.ci_timeout_seconds):
        return  # still within budget — no-op, zero writes

    ok = await transition_run(
        session,
        run_id=run.id,
        expected_version=run.version,
        new_status=RunStatus.PARKED,
        extra={"parked_reason": ParkedReason.CI_TIMEOUT.value},
    )
    if not ok:
        await _reject_lost_cas_unless_raced(session, run, "awaiting_ci->parked(ci_timeout)")
        return
    await alerts.run_parked(project.slug, run.id, ParkedReason.CI_TIMEOUT.value)


async def flip_project(
    session: AsyncSession,
    admin_ops: RepoOps,
    project: Project,
    *,
    to: ProjectLifecycle,
    alerts: AlertSink,
) -> bool:
    """The shared, idempotent lifecycle flip (SPEC §3.1; SPEC §6.3
    protection ordering) — `check_flip` below drives it automatically on
    first green; B3's manual-flip API drives it directly for repair, both
    directions.

    The guarded `UPDATE ... WHERE lifecycle = <the other value>` is the
    entire idempotency story: rowcount 0 means the project was already at
    `to` (or some caller's stale view named the wrong `from`), and this
    returns `False` having done nothing else — no protection call, no
    events, no alert. Only a won guard applies the matching protection
    (`apply_strict_protection` on the upgrade to `oracle_gated`,
    `apply_partial_protection` on the downgrade to `bootstrap`), writes the
    two `project_events` rows (`lifecycle_flipped`, `protection_applied`),
    and — upgrade only — fires `alerts.project_flipped` (a downgrade is
    manual repair, not the doctrine-#1 milestone worth alerting on).
    """
    other = (
        ProjectLifecycle.BOOTSTRAP
        if to == ProjectLifecycle.ORACLE_GATED
        else ProjectLifecycle.ORACLE_GATED
    )
    result = await session.execute(
        update(Project)
        .where(Project.id == project.id, Project.lifecycle == other.value)
        .values(lifecycle=to.value)
    )
    if result.rowcount != 1:
        return False

    if to == ProjectLifecycle.ORACLE_GATED:
        await admin_ops.apply_strict_protection(project.unattended_branch)
        protection_level = "strict"
    else:
        await admin_ops.apply_partial_protection(project.unattended_branch)
        protection_level = "partial"

    session.add(
        ProjectEvent(
            project_id=project.id,
            event_type="lifecycle_flipped",
            payload={"from": other.value, "to": to.value},
        )
    )
    session.add(
        ProjectEvent(
            project_id=project.id,
            event_type="protection_applied",
            payload={"level": protection_level},
        )
    )

    if to == ProjectLifecycle.ORACLE_GATED:
        await alerts.project_flipped(project.slug)

    return True


async def check_flip(
    session: AsyncSession,
    ops: RepoOps,
    admin_ops: RepoOps,
    run: Run,
    project: Project,
    *,
    alerts: AlertSink,
) -> None:
    """Plan Behavioral decision 4, the flip's observation site: a bootstrap
    project's `awaiting_review` run PR is where the just-onboarded
    `werft-oracle` workflow first lands and first runs (it is created by
    this very PR), so this is where the automatic flip (SPEC §3.1) is
    actually observed. Only a decisive `SUCCESS` acts; `PENDING`, `ABSENT`,
    and `FAILURE` are all no-ops here — there is no timeout on a review
    wait (SPEC §3.2: "a review may wait indefinitely") and a red oracle is
    simply not yet the milestone. The run itself is never touched either
    way — the flip is a project-level fact, not a run transition — so it
    stays `awaiting_review` in the operator's queue regardless.
    """
    try:
        pr = await ops.get_pr(run.pr_number)
    except GitHubUnavailable as exc:
        logger.warning("ci_watch.check_flip_get_pr_unavailable", run_id=str(run.id), error=str(exc))
        return
    if pr is None:
        return

    try:
        state = await ops.oracle_check(pr.head_sha)
    except GitHubUnavailable as exc:
        logger.warning("ci_watch.check_flip_oracle_unavailable", run_id=str(run.id), error=str(exc))
        return

    if state != CheckState.SUCCESS:
        return

    await flip_project(session, admin_ops, project, to=ProjectLifecycle.ORACLE_GATED, alerts=alerts)
