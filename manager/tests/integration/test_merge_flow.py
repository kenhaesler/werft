"""`orchestrator/merge_flow.py` against a real DB (SPEC §6.1 branch
lifecycle; SPEC §6.2 merge mechanics; SPEC §3.2 edges; plan Behavioral
decisions 1, 5, 6).

Seeding follows `test_ci_watch.py`/`test_finalize.py`'s style: `merging` is
staged with a direct raw-SQL `INSERT` (the `BEFORE UPDATE` transition
trigger never fires on insert, so any initial status is legal to seed —
same technique `test_finalize.py` uses for `running`), rather than driving
through the legal `awaiting_ci -> merging` / `awaiting_review -> merging`
edges, since nothing here needs that history.
"""

import uuid
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select, text

from werft.db.models import BacklogItem, Project, Run, RunEvent
from werft.db.transitions import transition_run
from werft.domain.runs import RunStatus
from werft.github.client import GitHubUnavailable
from werft.github.ops import MergeBlocked, MergeShaMismatch, PullRequest
from werft.orchestrator.merge_flow import advance_merging, cleanup_terminal

# -- fakes / spies ------------------------------------------------------------


class FakeRepoOps:
    """Duck-typed `RepoOps` for merge-flow scenarios.

    `get_pr` replays a scripted list of `PullRequest` snapshots, one per
    call, repeating the last entry once exhausted — a merge-flow scenario
    often spans two `advance_merging` calls (e.g. bootstrap's
    update-branch-then-merge sequence), and each call needs its own fresh
    read. `update_branch`/`squash_merge`/`close_pr`/`delete_ref` each
    return one canned result or raise one canned exception on every call,
    and record every call made.

    `oracle_check` is a hard trap, not a value to assert on: bootstrap
    merging never consults the oracle (decision 5 — no check to wait for),
    and oracle_gated merging arrives already-green and never rechecks it
    either. Calling it at all from `merge_flow.py` is a test failure.
    """

    def __init__(
        self,
        *,
        prs: list[PullRequest] | None = None,
        update_branch_error: Exception | None = None,
        squash_merge_result: str | None = None,
        squash_merge_error: Exception | None = None,
        close_pr_error: Exception | None = None,
        delete_ref_error: Exception | None = None,
    ) -> None:
        self._prs = list(prs or [])
        self._update_branch_error = update_branch_error
        self._squash_merge_result = squash_merge_result
        self._squash_merge_error = squash_merge_error
        self._close_pr_error = close_pr_error
        self._delete_ref_error = delete_ref_error
        self.get_pr_calls: list[int] = []
        self.update_branch_calls: list[tuple[int, str]] = []
        self.squash_merge_calls: list[tuple[int, str, str]] = []
        self.close_pr_calls: list[int] = []
        self.delete_ref_calls: list[str] = []
        self.oracle_check_calls: list[str] = []

    async def get_pr(self, number: int) -> PullRequest | None:
        self.get_pr_calls.append(number)
        if not self._prs:
            return None
        index = min(len(self.get_pr_calls) - 1, len(self._prs) - 1)
        return self._prs[index]

    async def update_branch(self, number: int, expected_head_sha: str) -> None:
        self.update_branch_calls.append((number, expected_head_sha))
        if self._update_branch_error is not None:
            raise self._update_branch_error

    async def squash_merge(self, number: int, head_sha: str, commit_title: str) -> str:
        self.squash_merge_calls.append((number, head_sha, commit_title))
        if self._squash_merge_error is not None:
            raise self._squash_merge_error
        assert self._squash_merge_result is not None
        return self._squash_merge_result

    async def close_pr(self, number: int) -> None:
        self.close_pr_calls.append(number)
        if self._close_pr_error is not None:
            raise self._close_pr_error

    async def delete_ref(self, branch: str) -> None:
        self.delete_ref_calls.append(branch)
        if self._delete_ref_error is not None:
            raise self._delete_ref_error

    async def oracle_check(self, ref: str):
        self.oracle_check_calls.append(ref)
        raise AssertionError("oracle_check must never be called from merge_flow.py (decisions 5/6)")


class SpyAlertSink:
    def __init__(self) -> None:
        self.run_parked_calls: list[tuple[str, uuid.UUID, str]] = []
        self.review_waiting_calls: list[tuple[str, uuid.UUID, str]] = []
        self.project_flipped_calls: list[str] = []
        self.auth_failure_calls: list[str] = []
        self.quota_exhausted_until_calls: list[tuple[str, datetime]] = []
        self.disk_threshold_calls: list[float] = []

    async def review_waiting(self, project_slug, run_id, pr_url) -> None:
        self.review_waiting_calls.append((project_slug, run_id, pr_url))

    async def run_parked(self, project_slug, run_id, reason) -> None:
        self.run_parked_calls.append((project_slug, run_id, reason))

    async def project_flipped(self, project_slug) -> None:
        self.project_flipped_calls.append(project_slug)

    async def auth_failure(self, provider) -> None:
        self.auth_failure_calls.append(provider)

    async def quota_exhausted_until(self, provider, until) -> None:
        self.quota_exhausted_until_calls.append((provider, until))

    async def disk_threshold(self, percent) -> None:
        self.disk_threshold_calls.append(percent)


def make_pr(
    number: int,
    *,
    head_sha: str = "deadbeef",
    state: str = "open",
    merged: bool = False,
    mergeable: bool | None = None,
    mergeable_state: str = "unknown",
) -> PullRequest:
    return PullRequest(
        number=number,
        state=state,
        merged=merged,
        head_ref=f"werft/run-{number}",
        head_sha=head_sha,
        base_ref="unattended",
        mergeable=mergeable,
        mergeable_state=mergeable_state,
        html_url=f"https://github.test/o/r/pull/{number}",
    )


# -- seeding -------------------------------------------------------------------


async def seed_project(session, *, lifecycle: str = "oracle_gated") -> Project:
    tag = uuid.uuid4().hex[:8]
    pid = (
        await session.execute(
            text(
                "INSERT INTO projects (slug, github_owner, github_repo, lifecycle) "
                "VALUES (:slug, 'o', :repo, :lifecycle) RETURNING id"
            ),
            {"slug": f"p-{tag}", "repo": f"r-{tag}", "lifecycle": lifecycle},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(Project, pid)


async def seed_backlog_item(session, project: Project, number: int) -> BacklogItem:
    bid = (
        await session.execute(
            text(
                "INSERT INTO backlog_items "
                "(project_id, github_issue_number, title, github_updated_at) "
                "VALUES (:p, :n, 't', now()) RETURNING id"
            ),
            {"p": project.id, "n": number},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(BacklogItem, bid)


async def seed_run(
    session,
    project: Project,
    item: BacklogItem,
    *,
    status: str = "merging",
    pr_number: int | None = 101,
    branch_name: str | None = None,
) -> Run:
    rid = (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status, provider, "
                "pr_number, branch_name) "
                "VALUES (:p, :b, :status, 'claude', :pr, :branch) RETURNING id"
            ),
            {
                "p": project.id,
                "b": item.id,
                "status": status,
                "pr": pr_number,
                "branch": branch_name,
            },
        )
    ).scalar_one()
    await session.commit()
    return await session.get(Run, rid)


async def fresh_run(session, run_id) -> Run:
    return await session.get(Run, run_id, populate_existing=True)


async def cleanup_events(session, run_id) -> list[RunEvent]:
    result = await session.execute(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.event_type == "cleanup")
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


# -- advance_merging: oracle_gated, merge succeeds directly --------------------


async def test_oracle_gated_merge_200_lands_merged_with_sha_and_deletes_branch(db_session) -> None:
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=101)
    ops = FakeRepoOps(
        prs=[make_pr(101, head_sha="cafef00d", mergeable=True, mergeable_state="clean")],
        squash_merge_result="feedbeef",
    )
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "merged"
    assert updated.merge_commit_sha == "feedbeef"
    assert ops.squash_merge_calls == [(101, "cafef00d", f"werft: {project.slug} run {run.id}")]
    assert ops.delete_ref_calls == [f"werft/run-{run.id}"]
    assert ops.oracle_check_calls == []


async def test_oracle_gated_405_dirty_parks_merge_conflict_and_alerts(db_session) -> None:
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=101)
    ops = FakeRepoOps(
        prs=[make_pr(101, mergeable=True, mergeable_state="dirty")],
        squash_merge_error=MergeBlocked(405, "not mergeable"),
    )
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "parked"
    assert updated.parked_reason == "merge_conflict"
    assert alerts.run_parked_calls == [(project.slug, run.id, "merge_conflict")]
    assert ops.update_branch_calls == []  # dirty never gets an update-branch attempt
    assert ops.delete_ref_calls == []


async def test_oracle_gated_405_behind_updates_branch_then_reenters_awaiting_ci(db_session) -> None:
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=101)
    ops = FakeRepoOps(
        prs=[make_pr(101, head_sha="aaa111", mergeable=True, mergeable_state="behind")],
        squash_merge_error=MergeBlocked(405, "not mergeable"),
    )
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "awaiting_ci"
    assert ops.update_branch_calls == [(101, "aaa111")]  # head-sha guard
    assert alerts.run_parked_calls == []


async def test_oracle_gated_405_blocked_state_also_updates_branch_and_reenters_awaiting_ci(
    db_session,
) -> None:
    """`mergeable_state` can legitimately be anything other than `dirty` on
    a 405 (behind/blocked/unstable/unknown) — decision 6 treats all of them
    the same way: only `dirty` is a real conflict."""
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=101)
    ops = FakeRepoOps(
        prs=[make_pr(101, head_sha="bbb222", mergeable=True, mergeable_state="blocked")],
        squash_merge_error=MergeBlocked(405, "not mergeable"),
    )
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "awaiting_ci"
    assert ops.update_branch_calls == [(101, "bbb222")]


async def test_oracle_gated_409_reenters_awaiting_ci_without_park_or_update_branch(
    db_session,
) -> None:
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=101)
    ops = FakeRepoOps(
        prs=[make_pr(101, mergeable=True, mergeable_state="clean")],
        squash_merge_error=MergeShaMismatch(409, "head moved"),
    )
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "awaiting_ci"
    assert ops.update_branch_calls == []  # 409 never triggers an update-branch
    assert alerts.run_parked_calls == []


async def test_oracle_gated_mergeable_none_leaves_state_untouched_with_zero_writes(
    db_session,
) -> None:
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=101)
    ops = FakeRepoOps(prs=[make_pr(101, mergeable=None, mergeable_state="unknown")])
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "merging"
    assert updated.version == run.version  # zero writes
    assert ops.squash_merge_calls == []


# -- advance_merging: bootstrap ------------------------------------------------


async def test_bootstrap_behind_updates_branch_stays_merging_then_merges_on_next_tick_no_oracle(
    db_session,
) -> None:
    """Two consecutive `advance_merging` calls model two ticks: the first
    observes `behind` and fires the async update-branch; the second (a
    later tick) observes the now-clean, moved-on head and merges. Neither
    tick ever touches `oracle_check` — bootstrap has no check to wait for.
    """
    project = await seed_project(db_session, lifecycle="bootstrap")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=202)
    ops = FakeRepoOps(
        prs=[
            make_pr(202, head_sha="old111", mergeable=True, mergeable_state="behind"),
            make_pr(202, head_sha="new222", mergeable=True, mergeable_state="clean"),
        ],
        squash_merge_result="mergedsha1",
    )
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    mid = await fresh_run(db_session, run.id)
    assert mid.status == "merging"
    assert ops.update_branch_calls == [(202, "old111")]
    assert ops.squash_merge_calls == []

    await advance_merging(db_session, ops, mid, project, alerts=alerts)
    await db_session.commit()

    final = await fresh_run(db_session, run.id)
    assert final.status == "merged"
    assert final.merge_commit_sha == "mergedsha1"
    assert ops.squash_merge_calls == [(202, "new222", f"werft: {project.slug} run {run.id}")]
    assert ops.oracle_check_calls == []  # the whole point of this test


async def test_bootstrap_dirty_parks_merge_blocked_spec_literal_reason(db_session) -> None:
    project = await seed_project(db_session, lifecycle="bootstrap")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=202)
    ops = FakeRepoOps(prs=[make_pr(202, mergeable=False, mergeable_state="dirty")])
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "parked"
    assert updated.parked_reason == "merge_blocked"  # bootstrap's named reason, not merge_conflict
    assert alerts.run_parked_calls == [(project.slug, run.id, "merge_blocked")]
    assert ops.squash_merge_calls == []
    assert ops.update_branch_calls == []
    assert ops.oracle_check_calls == []


async def test_bootstrap_mergeable_none_leaves_state_untouched_with_zero_writes(db_session) -> None:
    project = await seed_project(db_session, lifecycle="bootstrap")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=202)
    ops = FakeRepoOps(prs=[make_pr(202, mergeable=None, mergeable_state="unknown")])
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "merging"
    assert updated.version == run.version
    assert ops.squash_merge_calls == []
    assert ops.update_branch_calls == []
    assert ops.oracle_check_calls == []


async def test_bootstrap_409_on_merge_stays_merging_for_next_tick_retry(db_session) -> None:
    project = await seed_project(db_session, lifecycle="bootstrap")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=202)
    ops = FakeRepoOps(
        prs=[make_pr(202, mergeable=True, mergeable_state="clean")],
        squash_merge_error=MergeShaMismatch(409, "base moved"),
    )
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "merging"
    assert updated.version == run.version
    assert alerts.run_parked_calls == []


# -- advance_merging: PR gone / merged out-of-band -----------------------------


async def test_pr_gone_advances_to_failed(db_session) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=101)
    ops = FakeRepoOps(prs=[])
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "failed"
    assert updated.error_message


async def test_pr_closed_not_merged_advances_to_failed(db_session) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=101)
    ops = FakeRepoOps(prs=[make_pr(101, state="closed", merged=False)])
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "failed"


async def test_pr_merged_out_of_band_lands_merged_with_null_sha(db_session) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=101)
    ops = FakeRepoOps(prs=[make_pr(101, state="closed", merged=True)])
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "merged"
    assert updated.merge_commit_sha is None  # unknown; never guessed
    assert ops.delete_ref_calls == [f"werft/run-{run.id}"]


# -- advance_merging: GitHubUnavailable containment ----------------------------


async def test_get_pr_unavailable_leaves_state_untouched_and_does_not_raise(db_session) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=101)

    class RaisingOps(FakeRepoOps):
        async def get_pr(self, number: int) -> PullRequest | None:
            self.get_pr_calls.append(number)
            raise GitHubUnavailable(503, "service unavailable")

    ops = RaisingOps()
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, run, project, alerts=alerts)  # must not raise
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "merging"
    assert updated.version == run.version


async def test_squash_merge_unavailable_leaves_state_untouched_and_does_not_raise(
    db_session,
) -> None:
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=101)
    ops = FakeRepoOps(
        prs=[make_pr(101, mergeable=True, mergeable_state="clean")],
        squash_merge_error=GitHubUnavailable(500, "boom"),
    )
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, run, project, alerts=alerts)  # must not raise
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "merging"
    assert updated.version == run.version


async def test_delete_ref_unavailable_after_merge_does_not_undo_the_merge_transition(
    db_session,
) -> None:
    """The merge already landed on GitHub the moment `squash_merge` returned
    200; a transient failure tearing down the now-obsolete branch must not
    unwind that. `cleanup_terminal`'s sweep is the backstop."""
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=101)
    ops = FakeRepoOps(
        prs=[make_pr(101, mergeable=True, mergeable_state="clean")],
        squash_merge_result="feedbeef",
        delete_ref_error=GitHubUnavailable(503, "unavailable"),
    )
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, run, project, alerts=alerts)  # must not raise
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "merged"  # the transition stands despite the failed delete
    assert updated.merge_commit_sha == "feedbeef"


# -- advance_merging: lost CAS races a concurrent operator cancel --------------


async def test_merge_success_racing_an_out_of_band_cancel_returns_cleanly_without_raising(
    db_session,
) -> None:
    """`merging -> canceled` is itself a legal, routine edge — an operator
    can cancel a run while its merge is in flight. This seeds `merging`,
    captures a stale view, transitions the *real* row to `canceled` out
    from under it, then calls `advance_merging` with the stale view. The
    `merging -> merged` CAS must lose (stale version), and the lost-CAS
    handler must recognize the race and return cleanly — the canceled
    status stands, and no branch delete happens for a merge that never
    actually landed.
    """
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, pr_number=101)
    stale_view = SimpleNamespace(id=run.id, version=run.version, pr_number=run.pr_number)

    canceled = await transition_run(
        db_session, run_id=run.id, expected_version=run.version, new_status=RunStatus.CANCELED
    )
    assert canceled  # sanity: merging -> canceled really is a legal edge
    await db_session.commit()

    ops = FakeRepoOps(
        prs=[make_pr(101, mergeable=True, mergeable_state="clean")],
        squash_merge_result="feedbeef",
    )
    alerts = SpyAlertSink()

    await advance_merging(db_session, ops, stale_view, project, alerts=alerts)  # must not raise
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "canceled"  # the race's outcome stands, untouched
    assert ops.delete_ref_calls == []  # no branch delete for a merge that never landed


# -- cleanup_terminal -----------------------------------------------------------


async def test_cleanup_canceled_with_pr_closes_and_deletes_once_then_is_a_no_op(db_session) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="canceled", pr_number=101)
    ops = FakeRepoOps()

    await cleanup_terminal(db_session, ops, run)
    await db_session.commit()

    assert ops.close_pr_calls == [101]
    assert ops.delete_ref_calls == [f"werft/run-{run.id}"]
    events = await cleanup_events(db_session, run.id)
    assert len(events) == 1
    assert events[0].payload == {"closed_pr": 101, "deleted_branch": f"werft/run-{run.id}"}

    # second call: fully idempotent — the prior cleanup event is the guard.
    await cleanup_terminal(db_session, ops, run)
    await db_session.commit()

    assert ops.close_pr_calls == [101]  # still exactly one
    assert ops.delete_ref_calls == [f"werft/run-{run.id}"]  # still exactly one
    assert len(await cleanup_events(db_session, run.id)) == 1  # still exactly one


async def test_cleanup_canceled_without_pr_number_does_nothing(db_session) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="canceled", pr_number=None)
    ops = FakeRepoOps()

    await cleanup_terminal(db_session, ops, run)
    await db_session.commit()

    assert ops.close_pr_calls == []
    assert ops.delete_ref_calls == []
    assert await cleanup_events(db_session, run.id) == []


async def test_cleanup_merged_deletes_branch_only_no_close_pr_call(db_session) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="merged", pr_number=101)
    ops = FakeRepoOps()

    await cleanup_terminal(db_session, ops, run)
    await db_session.commit()

    assert ops.close_pr_calls == []  # already closed by GitHub's own merge
    assert ops.delete_ref_calls == [f"werft/run-{run.id}"]
    events = await cleanup_events(db_session, run.id)
    assert len(events) == 1
    assert events[0].payload == {"deleted_branch": f"werft/run-{run.id}"}


async def test_cleanup_parked_is_never_touched(db_session) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="parked", pr_number=101)
    ops = FakeRepoOps()

    await cleanup_terminal(db_session, ops, run)
    await db_session.commit()

    assert ops.close_pr_calls == []
    assert ops.delete_ref_calls == []
    assert await cleanup_events(db_session, run.id) == []


async def test_cleanup_github_unavailable_writes_no_event_so_a_later_sweep_retries(
    db_session,
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="canceled", pr_number=101)
    ops = FakeRepoOps(delete_ref_error=GitHubUnavailable(503, "unavailable"))

    await cleanup_terminal(db_session, ops, run)  # must not raise
    await db_session.commit()

    assert ops.close_pr_calls == [101]  # attempted before the failing delete
    assert await cleanup_events(db_session, run.id) == []  # nothing written — safe to retry
