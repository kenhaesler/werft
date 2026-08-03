"""Merge flow + terminal cleanup (SPEC §6.1 branch lifecycle; SPEC §6.2 merge
mechanics; SPEC §3.2 edges; plan Behavioral decisions 1, 5, 6).

`advance_merging` is one tick's worth of decision for one `merging` run, and
it is the one place `strict_serialized` (SPEC §6.2) actually happens. The
two lifecycles get genuinely different decision tables, both driven off the
same `PullRequest` snapshot (one `get_pr` per call — never more):

- **`oracle_gated`** (decision 6) arrives at `merging` from a *already-green*
  oracle check (`ci_watch.advance_awaiting_ci`'s `SUCCESS` edge) — this
  module never calls `oracle_check` itself. It tries the squash-merge
  directly: `200` lands `merged`; a `405` (`MergeBlocked`) means GitHub
  wouldn't merge it at all, and `mergeable_state` disambiguates *why* —
  `dirty` is a real conflict (`parked/merge_conflict` + alert); `behind`
  means the base moved, so an update-branch is due and the *updated* head
  must re-earn green (`merging -> awaiting_ci`, SPEC §6.2); and anything
  else (`clean`/`blocked`/`unstable`/unknown) is a *permanent* block —
  squash merges disabled, an org ruleset — which parks as `merge_blocked`
  with an alert rather than ping-ponging `merging -> awaiting_ci ->
  merging` forever on an unchanging head; a `409` (`MergeShaMismatch`) means the head itself moved
  since the last read — same destination, no update-branch needed (the
  next `awaiting_ci` pass reads the fresh head on its own). `mergeable is
  None` (GitHub still computing it) is a pure no-op: stay `merging`, next
  tick re-reads.
- **`bootstrap`** (decision 5) has no check to wait for at all — the
  operator's accept *is* the merge decision, and GitHub's `update-branch`
  being async (202, background job) means "update then merge" has to be a
  tick-driven decision table rather than one inline await chain: `behind`
  fires the update and stays `merging` (next tick re-reads the refreshed
  PR); `mergeable is False` (`dirty`) parks as `merge_blocked` — SPEC
  §6.2's named reason for a bootstrap conflict, distinct from
  oracle_gated's `merge_conflict` (the two lifecycles never share a parked
  reason, so which one appears is itself diagnostic); otherwise the
  squash-merge is attempted directly, and a `409` (external base move,
  rare — merges are serialized per project) simply stays `merging` for the
  next tick to retry. A `405` (`MergeBlocked`) *on that attempt* — GitHub's
  `mergeable` is computed async, so a clean-at-read snapshot can still be
  followed by a conflicting base push before the merge call lands, and a
  squash-disabled repo 405s every time regardless of `mergeable` — parks as
  `merge_blocked` the same as a pre-flight `dirty` read: SPEC §6.2 gives
  bootstrap a binary outcome, merge or park, never an indefinite retry.
  **No call to `oracle_check` ever happens on this path** — asserting that
  absence is exactly as load-bearing as asserting any transition, since a
  stray oracle read here would silently convert an operator's accept into
  a CI wait that bootstrap has no mechanism to satisfy.

A `PullRequest.merged` seen `True` (merged out-of-band, e.g. an operator
merged it by hand on GitHub) short-circuits both tables: it's a merge
success either way, just one this process didn't drive — landed as
`merged` with `merge_commit_sha` left `NULL` (unknown; logged) rather than
guessed. A gone PR (404) or a `closed`-but-not-`merged` PR is the same
infra edge `ci_watch.py` uses for `awaiting_ci` (SPEC §3.2): `merging ->
failed`, out-of-band and never a park.

A won `merging -> merged` CAS is followed by `_land_merged`'s two pieces of
after-the-fact housekeeping, both strictly *after* the CAS: retiring the
backlog item (`is_eligible=False` in the same transaction, plus a
best-effort `remove_label` — without it, every merged milestone re-queues
itself on the next 60 s intake, since a run PR merges into `unattended`
rather than the default branch and GitHub therefore closes nothing), and
the run branch's delete. Neither may unwind the merge: a failing GitHub
call there is caught, logged, and left to converge (`sync_backlog`'s
absent-from-ready-set sweep for the label, `cleanup_terminal`'s sweep for
the branch). See `_land_merged`'s docstring for which error classes each
catch covers and why.

`cleanup_terminal` is decision 1, verbatim: on a `canceled` run *with* a
PR, close it and delete `werft/run-<id>` (SPEC §6.1: ephemeral, deleted on
merge/terminal); on `merged`, delete the branch only (the PR is already
closed by GitHub's own merge). `parked` is never touched here — SPEC §3.2:
`parked` always admits a human requeue, and a requeue's next dispatch
force-resets the same branch, so tearing it down now would just make the
next attempt recreate it. Idempotence is a single guard: a prior `cleanup`
`run_events` row for this run means there is nothing left to do — no new
column, no ops calls, no new event. A `GitHubUnavailable` anywhere in the
attempt aborts without writing the event, so a later sweep re-drives the
whole thing (both `close_pr` and `delete_ref` are naturally idempotent, so
a partial retry is always safe).
"""

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from werft.db.models import BacklogItem, Project, Run, RunEvent
from werft.db.transitions import transition_run
from werft.domain.projects import ProjectLifecycle
from werft.domain.runs import ParkedReason, RunStatus, run_branch_name
from werft.github.client import GitHubApiError, GitHubUnavailable
from werft.github.ops import READY_LABEL, MergeBlocked, MergeShaMismatch, PullRequest, RepoOps
from werft.observe.alerts import AlertSink

logger = structlog.get_logger(__name__)

#: Terminal statuses `cleanup_terminal` ever acts on (decision 1). `parked`
#: is deliberately excluded — SPEC §3.2: it always admits a human requeue.
_CLEANUP_STATUSES = frozenset({RunStatus.CANCELED.value, RunStatus.MERGED.value})


def _branch_name(run: Run) -> str:
    """`run.branch_name` when a dispatcher (T7) recorded one; otherwise the
    deterministic name every run's branch gets (SPEC §6.1: `werft/run-<id>`),
    derived from `domain.runs.run_branch_name` — never re-spelled here."""
    return run.branch_name or run_branch_name(run.id)


def _commit_title(project: Project, run: Run) -> str:
    """The squash-merge commit title: deterministic, human-legible, and
    stable across retries (the same run always produces the same title, so
    a re-driven merge attempt after a crash is not distinguishable from a
    first try by this field alone)."""
    return f"werft: {project.slug} run {run.id}"


async def _reject_lost_cas_unless_raced(session: AsyncSession, run: Run, edge: str) -> None:
    """Same shape as `ci_watch._reject_lost_cas_unless_raced`: a lost CAS out
    of `merging` is only ever a race with a concurrent, already-completed
    advance (an operator's `merging -> canceled` cancel is the one legal
    such edge) — re-reading and finding the row genuinely elsewhere means
    that other call's outcome stands. Still `merging` despite the lost CAS
    is a version mismatch that should be impossible under the single-writer
    design, and is a bug worth raising for."""
    current = await session.get(Run, run.id, populate_existing=True)
    if current.status != RunStatus.MERGING.value:
        return
    raise RuntimeError(f"merge_flow: lost CAS race on run {run.id} ({edge})")


async def _fail_gone(session: AsyncSession, run: Run, message: str) -> None:
    """`merging -> failed`: the PR vanished or closed without merging,
    out-of-band (SPEC §3.2's infra edge — never a park, same treatment
    `ci_watch.advance_awaiting_ci` gives a gone PR)."""
    ok = await transition_run(
        session,
        run_id=run.id,
        expected_version=run.version,
        new_status=RunStatus.FAILED,
        extra={"error_message": message},
    )
    if not ok:
        await _reject_lost_cas_unless_raced(session, run, "merging->failed")


async def _park(
    session: AsyncSession, run: Run, project: Project, reason: ParkedReason, *, alerts: AlertSink
) -> None:
    """`merging -> parked` with the given reason, alerting only on a won
    CAS (SPEC §9's generic "park" alert covers every parked reason, not
    just the ones a specific handler was written for first)."""
    ok = await transition_run(
        session,
        run_id=run.id,
        expected_version=run.version,
        new_status=RunStatus.PARKED,
        extra={"parked_reason": reason.value},
    )
    if not ok:
        await _reject_lost_cas_unless_raced(session, run, f"merging->parked({reason.value})")
        return
    await alerts.run_parked(project.slug, run.id, reason.value)


async def _land_merged(
    session: AsyncSession, ops: RepoOps, run: Run, *, merge_commit_sha: str | None
) -> None:
    """`merging -> merged`, then retire the backlog item and tear the branch
    down — everything strictly *after* the CAS has won.

    **Retiring the item is not optional bookkeeping.** A run PR merges into
    `project.unattended_branch`, never the repository's *default* branch, so
    GitHub's own issue auto-close never fires for it: the issue stays open
    and still labeled. `sync_backlog` only ever marks an item ineligible by
    finding it *absent* from the ready set, and a `merged` run sits outside
    `ux_runs_one_active_per_item`'s `WHERE status NOT IN
    ('merged','canceled')` predicate — so without the `is_eligible=False`
    write below, the next 60 s `intake` INSERTs a *second* `queued` run for
    work that is already merged, forever. That write shares this
    transaction with the CAS, so it is exactly as durable as the merge
    itself and is the actual guarantee.

    The label removal is the operator-visible echo of that flag, and is
    strictly best-effort: **every** `GitHubApiError` (the whole family, not
    just the transient one) is caught, because nothing GitHub says about a
    label may unwind a merge that has already landed. `sync_backlog`'s
    absent-from-ready-set sweep converges the rest on its own. The branch
    delete is best-effort for the same reason (the merge is real; only
    Werft's bookkeeping of the branch is stale) — `cleanup_terminal`'s sweep
    is its retry.
    """
    ok = await transition_run(
        session,
        run_id=run.id,
        expected_version=run.version,
        new_status=RunStatus.MERGED,
        extra={"merge_commit_sha": merge_commit_sha},
    )
    if not ok:
        await _reject_lost_cas_unless_raced(session, run, "merging->merged")
        return

    await session.execute(
        update(BacklogItem).where(BacklogItem.id == run.backlog_item_id).values(is_eligible=False)
    )
    item = await session.get(BacklogItem, run.backlog_item_id)
    if item is not None:
        try:
            await ops.remove_label(item.github_issue_number, READY_LABEL)
        except GitHubApiError as exc:
            logger.warning(
                "merge_flow.remove_label_failed",
                run_id=str(run.id),
                issue=item.github_issue_number,
                error=str(exc),
            )

    branch = _branch_name(run)
    try:
        await ops.delete_ref(branch)
    except GitHubUnavailable as exc:
        logger.warning(
            "merge_flow.delete_ref_unavailable", run_id=str(run.id), branch=branch, error=str(exc)
        )


async def _advance_bootstrap_merging(
    session: AsyncSession,
    ops: RepoOps,
    run: Run,
    project: Project,
    pr: PullRequest,
    *,
    alerts: AlertSink,
) -> None:
    """Plan Behavioral decision 5, verbatim. No branch here ever calls
    `oracle_check` — bootstrap has no check to wait for; the operator's
    accept already *is* the merge decision. Bootstrap's outcome here is
    binary (SPEC §6.2): merge, or park as `merge_blocked` — never an
    indefinite retry, so a `405` on the merge attempt itself (not just a
    pre-flight `dirty` read) also parks."""
    if pr.mergeable is None:
        return  # still computing; next tick re-reads

    if pr.mergeable_state == "behind":
        try:
            await ops.update_branch(run.pr_number, pr.head_sha)
        except GitHubUnavailable as exc:
            logger.warning(
                "merge_flow.update_branch_unavailable", run_id=str(run.id), error=str(exc)
            )
        except MergeShaMismatch:
            pass  # stale guard; next tick re-reads the (now different) head and re-decides
        return  # stay merging either way — the async update needs a later tick to observe

    if pr.mergeable is False:
        await _park(session, run, project, ParkedReason.MERGE_BLOCKED, alerts=alerts)
        return

    try:
        merge_sha = await ops.squash_merge(run.pr_number, pr.head_sha, _commit_title(project, run))
    except GitHubUnavailable as exc:
        logger.warning("merge_flow.squash_merge_unavailable", run_id=str(run.id), error=str(exc))
        return
    except MergeShaMismatch:
        return  # 409: external base move (rare, serialized per project) — next tick retries
    except MergeBlocked:
        # A 405 on the attempt itself, not just a pre-flight `dirty` read:
        # `mergeable` is computed async (clean-at-read, conflicting by the
        # time of the call) or squash merging is disabled on the repo
        # entirely. Bootstrap's outcome is binary (SPEC §6.2) — park rather
        # than retry forever.
        await _park(session, run, project, ParkedReason.MERGE_BLOCKED, alerts=alerts)
        return

    await _land_merged(session, ops, run, merge_commit_sha=merge_sha)


async def _advance_oracle_gated_merging(
    session: AsyncSession,
    ops: RepoOps,
    run: Run,
    project: Project,
    pr: PullRequest,
    *,
    alerts: AlertSink,
) -> None:
    """Plan Behavioral decision 6, verbatim. Arrived-from-green: the oracle
    was already observed `SUCCESS` by `ci_watch.advance_awaiting_ci` before
    this run ever reached `merging`, so this function tries the merge
    directly rather than pre-checking `mergeable`/`mergeable_state` the way
    bootstrap does."""
    if pr.mergeable is None:
        return  # still computing; next tick re-reads

    try:
        merge_sha = await ops.squash_merge(run.pr_number, pr.head_sha, _commit_title(project, run))
    except GitHubUnavailable as exc:
        logger.warning("merge_flow.squash_merge_unavailable", run_id=str(run.id), error=str(exc))
        return
    except MergeShaMismatch:
        # 409: the head itself moved since this read — no update-branch
        # needed, just re-enter awaiting_ci so the fresh head re-earns green.
        ok = await transition_run(
            session, run_id=run.id, expected_version=run.version, new_status=RunStatus.AWAITING_CI
        )
        if not ok:
            await _reject_lost_cas_unless_raced(session, run, "merging->awaiting_ci(409)")
        return
    except MergeBlocked:
        if pr.mergeable_state == "dirty":
            await _park(session, run, project, ParkedReason.MERGE_CONFLICT, alerts=alerts)
            return
        if pr.mergeable_state != "behind":
            # A 405 that is neither a conflict (`dirty`) nor a moved base
            # (`behind`) is permanent from Werft's side: squash merges are
            # disabled on the repo, or an org ruleset requires something the
            # doctrine-#1 protection PUT cannot satisfy. Reading it as "the
            # base moved" livelocks — update-branch leaves the head sha
            # unchanged, `advance_awaiting_ci` reads the same green oracle
            # and CASes straight back to `merging`, and each re-entry resets
            # the `ci_timeout` epoch, so not even the timeout backstop can
            # break the cycle. Park with an operator signal instead, the
            # same binary outcome `_advance_bootstrap_merging`'s own 405
            # guard takes.
            await _park(session, run, project, ParkedReason.MERGE_BLOCKED, alerts=alerts)
            return
        # behind: the genuine base-moved path — update, then the updated
        # head must re-earn green (SPEC §6.2).
        try:
            await ops.update_branch(run.pr_number, pr.head_sha)
        except GitHubUnavailable as exc:
            logger.warning(
                "merge_flow.update_branch_unavailable", run_id=str(run.id), error=str(exc)
            )
            return
        except MergeShaMismatch:
            return  # stale guard; next tick re-reads and re-decides
        ok = await transition_run(
            session, run_id=run.id, expected_version=run.version, new_status=RunStatus.AWAITING_CI
        )
        if not ok:
            await _reject_lost_cas_unless_raced(session, run, "merging->awaiting_ci(405)")
        return

    await _land_merged(session, ops, run, merge_commit_sha=merge_sha)


async def advance_merging(
    session: AsyncSession, ops: RepoOps, run: Run, project: Project, *, alerts: AlertSink
) -> None:
    """One tick's worth of decision for one `merging` run (plan Behavioral
    decisions 5-6; SPEC §3.2 edges). See the module docstring for the full
    decision table and the `GitHubUnavailable` containment boundary.
    """
    try:
        pr = await ops.get_pr(run.pr_number)
    except GitHubUnavailable as exc:
        logger.warning("merge_flow.get_pr_unavailable", run_id=str(run.id), error=str(exc))
        return

    if pr is None:
        await _fail_gone(session, run, "PR gone out-of-band while merging")
        return

    if pr.merged:
        # Merged out-of-band (e.g. an operator merged by hand on GitHub) —
        # a merge success this process didn't drive. The real merge commit
        # sha isn't in `PullRequest` at all; leave it NULL rather than guess.
        logger.info("merge_flow.merged_out_of_band", run_id=str(run.id), pr_number=run.pr_number)
        await _land_merged(session, ops, run, merge_commit_sha=None)
        return

    if pr.state == "closed":
        await _fail_gone(session, run, "PR closed without merging while merging")
        return

    if project.lifecycle == ProjectLifecycle.BOOTSTRAP:
        await _advance_bootstrap_merging(session, ops, run, project, pr, alerts=alerts)
    else:
        await _advance_oracle_gated_merging(session, ops, run, project, pr, alerts=alerts)


async def _has_cleanup_event(session: AsyncSession, run_id: UUID) -> bool:
    result = await session.execute(
        select(RunEvent.id)
        .where(RunEvent.run_id == run_id, RunEvent.event_type == "cleanup")
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def cleanup_terminal(session: AsyncSession, ops: RepoOps, run: Run) -> None:
    """Plan Behavioral decision 1, verbatim: terminal-path PR/branch
    cleanup (SPEC §6.1/§6.2 — "no CI-burning zombies"). `parked` runs are
    never touched (SPEC §3.2: `parked` always admits a human requeue, and
    the next dispatch force-resets this same branch). Safe to re-run: a
    prior `cleanup` `run_events` row for this run is the whole idempotence
    story, checked before any GitHub call is made.
    """
    if run.status not in _CLEANUP_STATUSES:
        return

    if await _has_cleanup_event(session, run.id):
        return

    payload: dict[str, Any] = {}
    try:
        if run.status == RunStatus.CANCELED.value:
            if run.pr_number is not None:
                await ops.close_pr(run.pr_number)
                payload["closed_pr"] = run.pr_number
                branch = _branch_name(run)
                await ops.delete_ref(branch)
                payload["deleted_branch"] = branch
        else:  # merged
            branch = _branch_name(run)
            await ops.delete_ref(branch)
            payload["deleted_branch"] = branch
    except GitHubUnavailable as exc:
        logger.warning(
            "merge_flow.cleanup_unavailable", run_id=str(run.id), status=run.status, error=str(exc)
        )
        return  # nothing written; a later sweep re-drives the whole attempt

    if payload:
        session.add(RunEvent(run_id=run.id, event_type="cleanup", payload=payload))
