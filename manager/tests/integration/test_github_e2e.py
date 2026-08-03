"""T5 end-to-end integration test (issue #22's acceptance scenarios): the
manager's own components — onboarding, backlog sync/intake, finalize,
CI-watch, the bootstrap flip, and merge/cleanup — driven together against a
real DB, composed exactly the way the orchestrator itself composes them
(`orchestrator/loop.py`'s per-unit handlers), rather than exercised one
function at a time the way `test_onboard.py`/`test_backlog.py`/
`test_finalize.py`/`test_ci_watch.py`/`test_merge_flow.py` each do.

The one new piece of test machinery this file needs beyond its siblings:
`FakeRepoOps`, a *stateful* fake `RepoOps` view onto one shared, in-memory
`RepoState` — branches, PRs, oracle results, protection level, and the
scripted ready-issue list — that mutates in place the way a real GitHub repo
would (`open_pr` adopts an existing open PR for the same head/base instead
of creating a duplicate; `squash_merge` actually flips a PR to `merged`;
`update_branch` actually advances a PR's head sha and `mergeable_state`).
Every sibling test file's `FakeRepoOps` instead replays a short, fixed script
of canned return values — correct for testing one function in isolation, but
not expressive enough to prove a PR opened by `finalize.py` is the same PR
`ci_watch.py`/`merge_flow.py` later read back, mutated, and merged.

Two `FakeRepoOps` *views* are constructed per scenario onto one shared
`RepoState`, mirroring `onboard.py`'s own manager/admin permission split:
`ops` (manager permissions) and `admin_ops` (transient admin permissions,
`administration:write`) each keep their own call-log, even though both
mutate the same underlying repo state — this is what lets a test assert
"the manager view never called `apply_partial_protection`" while the admin
view did, the same load-bearing assertion `test_onboard.py` makes.

Seeding follows the rest of `tests/integration`'s style: raw SQL to reach a
status no legal ORM path can seed alone (there is no dispatcher yet — T7 —
so `queued -> claimed -> running` is driven directly via `transition_run`,
same gap `test_finalize.py`'s own seeding helpers paper over), and
`transition_run` for every edge that *is* legal (an operator's accept, a
project's lifecycle flip's downstream routing).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select, text

from werft.contracts.result import ResultStatus
from werft.db.models import BacklogItem, Project, ProjectEvent, Run, RunEvent
from werft.db.transitions import transition_run
from werft.domain.runs import RunStatus
from werft.github.client import ConditionalResult
from werft.github.ops import CheckState, MergeBlocked, MergeShaMismatch, PullRequest
from werft.orchestrator.backlog import intake, sync_backlog
from werft.orchestrator.ci_watch import advance_awaiting_ci, check_flip
from werft.orchestrator.finalize import NullQuota, finalize_attempt
from werft.orchestrator.merge_flow import advance_merging, cleanup_terminal
from werft.orchestrator.onboard import onboard_project
from werft.providers.base import Classification

# -- the stateful fake GitHub -------------------------------------------------


@dataclass
class _PR:
    number: int
    head_ref: str
    base_ref: str
    head_sha: str
    state: str = "open"
    merged: bool = False
    mergeable: bool | None = True
    mergeable_state: str = "clean"
    merge_commit_sha: str | None = None


class RepoState:
    """One in-memory repo — branches, PRs, oracle results, protection
    level, and the scripted ready-issue list — shared by every
    `FakeRepoOps` view onto it, the same way two GitHub tokens (manager,
    admin) act on one real repo."""

    def __init__(self) -> None:
        self.branches: dict[str, str] = {}
        self.prs: dict[int, _PR] = {}
        self._next_pr_number = 1000
        self.oracle_results: dict[str, CheckState] = {}
        self.protection_level: str | None = None
        self.ready_issues: list[dict] = []

    def new_pr_number(self) -> int:
        self._next_pr_number += 1
        return self._next_pr_number


def _to_pull_request(pr: _PR) -> PullRequest:
    return PullRequest(
        number=pr.number,
        state=pr.state,
        merged=pr.merged,
        head_ref=pr.head_ref,
        head_sha=pr.head_sha,
        base_ref=pr.base_ref,
        mergeable=pr.mergeable,
        mergeable_state=pr.mergeable_state,
        html_url=f"https://github.test/o/r/pull/{pr.number}",
    )


class FakeRepoOps:
    """Duck-typed `RepoOps`, mutating a shared `RepoState` the way GitHub
    itself would (module docstring). Every call is recorded on *this* view
    even though the state it mutates is shared — the manager/admin
    protection-call split (`test_onboard.py`'s own load-bearing assertion)
    depends on that.
    """

    def __init__(self, state: RepoState) -> None:
        self.state = state
        self.get_ref_sha_calls: list[str] = []
        self.ensure_branch_calls: list[tuple[str, str]] = []
        self.force_reset_ref_calls: list[tuple[str, str]] = []
        self.delete_ref_calls: list[str] = []
        self.open_pr_calls: list[tuple[str, str, str, str]] = []
        self.get_pr_calls: list[int] = []
        self.close_pr_calls: list[int] = []
        self.update_branch_calls: list[tuple[int, str]] = []
        self.squash_merge_calls: list[tuple[int, str, str]] = []
        self.oracle_check_calls: list[str] = []
        self.list_ready_issues_calls: int = 0
        self.ensure_label_calls: list[tuple[str, str]] = []
        self.apply_partial_protection_calls: list[str] = []
        self.apply_strict_protection_calls: list[str] = []

    # -- refs -----------------------------------------------------------
    async def get_ref_sha(self, branch: str) -> str | None:
        self.get_ref_sha_calls.append(branch)
        return self.state.branches.get(branch)

    async def ensure_branch(self, branch: str, from_sha: str) -> str:
        self.ensure_branch_calls.append((branch, from_sha))
        self.state.branches.setdefault(branch, from_sha)
        return self.state.branches[branch]

    async def force_reset_ref(self, branch: str, sha: str) -> None:
        self.force_reset_ref_calls.append((branch, sha))
        self.state.branches[branch] = sha

    async def delete_ref(self, branch: str) -> None:
        self.delete_ref_calls.append(branch)
        self.state.branches.pop(branch, None)

    # -- pull requests ------------------------------------------------------
    async def open_pr(self, head: str, base: str, title: str, body: str) -> PullRequest:
        """Adopt-on-422, modeled directly: an existing *open* PR for the
        same head/base is returned as-is (no duplicate created) — exactly
        what a re-driven caller's real 422-then-adopt round trip converges
        on, whether the existing PR came from an earlier call this test
        made or was pre-seeded to model one that crashed before this test
        ever saw it."""
        self.open_pr_calls.append((head, base, title, body))
        for pr in self.state.prs.values():
            if pr.head_ref == head and pr.base_ref == base and pr.state == "open":
                return _to_pull_request(pr)
        number = self.state.new_pr_number()
        pr = _PR(number=number, head_ref=head, base_ref=base, head_sha=f"headsha-{number}")
        self.state.prs[number] = pr
        return _to_pull_request(pr)

    async def get_pr(self, number: int) -> PullRequest | None:
        self.get_pr_calls.append(number)
        pr = self.state.prs.get(number)
        return None if pr is None else _to_pull_request(pr)

    async def close_pr(self, number: int) -> None:
        self.close_pr_calls.append(number)
        pr = self.state.prs.get(number)
        if pr is not None:
            pr.state = "closed"

    async def update_branch(self, number: int, expected_head_sha: str) -> None:
        self.update_branch_calls.append((number, expected_head_sha))
        pr = self.state.prs[number]
        if pr.head_sha != expected_head_sha:
            raise MergeShaMismatch(422, "head moved since last read")
        pr.head_sha = f"{pr.head_sha}-updated"
        pr.mergeable = True
        pr.mergeable_state = "clean"

    async def squash_merge(self, number: int, head_sha: str, commit_title: str) -> str:
        self.squash_merge_calls.append((number, head_sha, commit_title))
        pr = self.state.prs[number]
        if pr.head_sha != head_sha:
            raise MergeShaMismatch(409, "head moved since last read")
        if pr.mergeable_state == "dirty":
            raise MergeBlocked(405, "not mergeable")
        merge_sha = f"mergedsha-{number}"
        pr.merged = True
        pr.state = "closed"
        pr.merge_commit_sha = merge_sha
        self.state.branches[pr.base_ref] = merge_sha  # base advances on a real merge
        return merge_sha

    # -- checks -------------------------------------------------------------
    async def oracle_check(self, ref: str) -> CheckState:
        self.oracle_check_calls.append(ref)
        return self.state.oracle_results.get(ref, CheckState.ABSENT)

    # -- backlog issues -------------------------------------------------------
    async def list_ready_issues(self) -> ConditionalResult:
        self.list_ready_issues_calls += 1
        return ConditionalResult(modified=True, data=list(self.state.ready_issues))

    # -- labels + protection --------------------------------------------------
    async def ensure_label(self, name: str, color: str) -> None:
        self.ensure_label_calls.append((name, color))

    async def apply_partial_protection(self, branch: str) -> None:
        self.apply_partial_protection_calls.append(branch)
        self.state.protection_level = "partial"

    async def apply_strict_protection(self, branch: str) -> None:
        self.apply_strict_protection_calls.append(branch)
        self.state.protection_level = "strict"


class SpyAlertSink:
    def __init__(self) -> None:
        self.review_waiting_calls: list[tuple[str, uuid.UUID, str]] = []
        self.run_parked_calls: list[tuple[str, uuid.UUID, str]] = []
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


def success_classification() -> Classification:
    # doctrine #1: a clean-exit success carries no attempt outcome yet — the
    # oracle hasn't spoken, so nothing here is a verdict.
    return Classification(outcome=None, status=ResultStatus.SUCCESS, detail="ok")


def make_ready_issue(number: int, *, title: str = "an item") -> dict:
    return {
        "number": number,
        "title": title,
        "body": "please add it",
        "labels": [{"name": "werft:ready"}],
        "updated_at": "2026-08-01T12:00:00Z",
    }


# -- seeding -------------------------------------------------------------------


async def seed_project(session, *, lifecycle: str = "bootstrap") -> Project:
    tag = uuid.uuid4().hex[:8]
    pid = (
        await session.execute(
            text(
                "INSERT INTO projects (slug, github_owner, github_repo, lifecycle) "
                "VALUES (:slug, 'acme', :repo, :lifecycle) RETURNING id"
            ),
            {"slug": f"p-{tag}", "repo": f"r-{tag}", "lifecycle": lifecycle},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(Project, pid)


async def seed_backlog_item(session, project: Project, number: int, *, title="an item"):
    bid = (
        await session.execute(
            text(
                "INSERT INTO backlog_items (project_id, github_issue_number, title, "
                "github_updated_at) VALUES (:p, :n, :t, now()) RETURNING id"
            ),
            {"p": project.id, "n": number, "t": title},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(BacklogItem, bid)


async def advance_queued_to_running(session, run: Run) -> Run:
    """`queued -> claimed -> running` via the same `transition_run` CAS
    T7's real dispatcher will use (T5 ships no dispatcher yet — this test
    drives the two legal pre-attempt edges directly), then seeds the one
    open `run_attempts` row a real dispatch would already have created
    before handing the container off — the same raw-SQL gap-filler
    `test_finalize.py`'s `seed_open_attempt` uses."""
    ok = await transition_run(
        session, run_id=run.id, expected_version=run.version, new_status=RunStatus.CLAIMED
    )
    assert ok
    await session.commit()
    claimed = await session.get(Run, run.id, populate_existing=True)
    ok = await transition_run(
        session, run_id=run.id, expected_version=claimed.version, new_status=RunStatus.RUNNING
    )
    assert ok
    await session.commit()
    await session.execute(
        text(
            "INSERT INTO run_attempts (run_id, attempt_no, provider, started_at) "
            "VALUES (:r, 1, 'claude', now() - interval '5 seconds')"
        ),
        {"r": run.id},
    )
    await session.commit()
    return await session.get(Run, run.id, populate_existing=True)


async def dispatch_to_running(
    session, project: Project, item: BacklogItem, *, attempt_count: int = 0, max_attempts: int = 3
) -> Run:
    rid = (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status, provider, "
                "attempt_count, max_attempts) VALUES (:p, :b, 'queued', 'claude', :ac, :ma) "
                "RETURNING id"
            ),
            {"p": project.id, "b": item.id, "ac": attempt_count, "ma": max_attempts},
        )
    ).scalar_one()
    await session.commit()
    run = await session.get(Run, rid)
    return await advance_queued_to_running(session, run)


async def fresh_run(session, run_id) -> Run:
    return await session.get(Run, run_id, populate_existing=True)


async def fresh_project(session, project_id) -> Project:
    return await session.get(Project, project_id, populate_existing=True)


async def project_events(session, project_id) -> list[ProjectEvent]:
    result = await session.execute(
        select(ProjectEvent)
        .where(ProjectEvent.project_id == project_id)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def run_events_of_type(session, run_id, event_type: str) -> list[RunEvent]:
    result = await session.execute(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.event_type == event_type)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


# -- scenario 1: bootstrap round trip -----------------------------------------


async def test_bootstrap_round_trip_onboard_through_merge_and_cleanup(db_session) -> None:
    """Onboard -> one scripted ready issue synced+intaken -> driven to
    `running` -> a pushed-success finalize opens the review PR -> the
    operator's accept CAS -> two `advance_merging` ticks (the PR starts
    `behind`, so the first tick only updates the branch; the second lands
    the merge) -> `cleanup_terminal`. The oracle is never consulted
    anywhere in this path — bootstrap has no check to wait for."""
    state = RepoState()
    state.branches["main"] = "main-sha-0"
    tag = uuid.uuid4().hex[:8]
    ops = FakeRepoOps(state)
    admin_ops = FakeRepoOps(state)
    alerts = SpyAlertSink()

    project = await onboard_project(
        db_session, ops, admin_ops, slug=f"e2e-{tag}", owner="acme", repo=f"widgets-{tag}"
    )
    await db_session.commit()
    assert project.lifecycle == "bootstrap"
    assert ops.apply_partial_protection_calls == []
    assert admin_ops.apply_partial_protection_calls == ["unattended"]
    assert state.protection_level == "partial"

    state.ready_issues = [make_ready_issue(42, title="add the widget")]
    await sync_backlog(db_session, ops, project)
    inserted = await intake(db_session, project)
    assert inserted == 1
    await db_session.commit()

    run = (await db_session.execute(select(Run).where(Run.project_id == project.id))).scalar_one()
    assert run.status == "queued"

    run = await advance_queued_to_running(db_session, run)
    assert run.status == "running"

    await finalize_attempt(
        db_session,
        ops,
        run,
        project,
        classification=success_classification(),
        pushed=True,
        quota=NullQuota(),
        alerts=alerts,
    )
    await db_session.commit()

    run = await fresh_run(db_session, run.id)
    assert run.status == "awaiting_review"
    assert run.pr_number is not None
    assert len(alerts.review_waiting_calls) == 1
    assert alerts.review_waiting_calls[0][:2] == (project.slug, run.id)

    pr = await ops.get_pr(run.pr_number)
    original_head_sha = pr.head_sha
    # simulate the base having moved since the PR's branch point, so the
    # merge tick below actually has an update-branch to do.
    state.prs[run.pr_number].mergeable_state = "behind"

    accepted = await transition_run(
        db_session, run_id=run.id, expected_version=run.version, new_status=RunStatus.MERGING
    )
    assert accepted  # operator accept, direct transition_run — as T6's endpoint will do
    await db_session.commit()
    run = await fresh_run(db_session, run.id)
    assert run.status == "merging"

    # tick 1: behind -> update_branch, stays merging
    await advance_merging(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()
    run = await fresh_run(db_session, run.id)
    assert run.status == "merging"
    assert ops.update_branch_calls == [(run.pr_number, original_head_sha)]
    assert ops.squash_merge_calls == []

    # tick 2: clean (post-update) -> merges
    await advance_merging(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()
    run = await fresh_run(db_session, run.id)
    assert run.status == "merged"
    assert run.merge_commit_sha is not None
    assert ops.delete_ref_calls == [f"werft/run-{run.id}"]
    assert ops.oracle_check_calls == []  # the whole point of this scenario

    await cleanup_terminal(db_session, ops, run)
    await db_session.commit()
    events = await run_events_of_type(db_session, run.id, "cleanup")
    assert len(events) == 1
    assert events[0].payload == {"deleted_branch": f"werft/run-{run.id}"}


# -- scenario 2: oracle_gated green -------------------------------------------


async def test_oracle_gated_green_awaiting_ci_to_merged(db_session) -> None:
    state = RepoState()
    ops = FakeRepoOps(state)
    alerts = SpyAlertSink()
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item = await seed_backlog_item(db_session, project, 1)
    run = await dispatch_to_running(db_session, project, item)

    await finalize_attempt(
        db_session,
        ops,
        run,
        project,
        classification=success_classification(),
        pushed=True,
        quota=NullQuota(),
        alerts=alerts,
    )
    await db_session.commit()
    run = await fresh_run(db_session, run.id)
    assert run.status == "awaiting_ci"
    assert alerts.review_waiting_calls == []  # oracle_gated never fires the bootstrap alert

    pr = await ops.get_pr(run.pr_number)
    state.oracle_results[pr.head_sha] = CheckState.SUCCESS

    await advance_awaiting_ci(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()
    run = await fresh_run(db_session, run.id)
    assert run.status == "merging"

    await advance_merging(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()
    run = await fresh_run(db_session, run.id)
    assert run.status == "merged"
    assert run.merge_commit_sha is not None


# -- scenario 3: deliberately red, budget spent -------------------------------


async def test_deliberately_red_budget_spent_parks_and_stays_parked(db_session) -> None:
    """Budget is already spent by the time the oracle reports red
    (`attempt_count == max_attempts - 1`, so this one failure exhausts it):
    `advance_awaiting_ci` parks it `ci_red`. Re-driving `advance_awaiting_ci`
    three more times on the same (now stale) view must never move the run
    anywhere else — in particular never `merging` — proven by comparing the
    run's `version`, captured as a plain int, across every re-drive."""
    state = RepoState()
    ops = FakeRepoOps(state)
    alerts = SpyAlertSink()
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item = await seed_backlog_item(db_session, project, 1)
    run = await dispatch_to_running(db_session, project, item, attempt_count=2, max_attempts=3)

    await finalize_attempt(
        db_session,
        ops,
        run,
        project,
        classification=success_classification(),
        pushed=True,
        quota=NullQuota(),
        alerts=alerts,
    )
    await db_session.commit()
    run = await fresh_run(db_session, run.id)
    assert run.status == "awaiting_ci"
    assert run.attempt_count == 2  # unchanged: finalize's pushed-success path never bumps it

    pr = await ops.get_pr(run.pr_number)
    state.oracle_results[pr.head_sha] = CheckState.FAILURE
    stale_view = SimpleNamespace(
        id=run.id,
        version=run.version,
        pr_number=run.pr_number,
        attempt_count=run.attempt_count,
        max_attempts=run.max_attempts,
    )

    await advance_awaiting_ci(db_session, ops, stale_view, project, alerts=alerts)
    await db_session.commit()
    run = await fresh_run(db_session, run.id)
    assert run.status == "parked"
    assert run.parked_reason == "ci_red"
    assert run.attempt_count == 3  # 2 + 1 == max_attempts: budget spent
    assert alerts.run_parked_calls == [(project.slug, run.id, "ci_red")]

    parked_version = run.version  # a plain int — never compared via an identity-mapped object

    for _ in range(3):  # exhaustive re-drive, per the acceptance scenario
        await advance_awaiting_ci(db_session, ops, stale_view, project, alerts=alerts)
        await db_session.commit()
        run = await fresh_run(db_session, run.id)
        assert run.status == "parked"  # never merging, never anywhere else
        assert run.version == parked_version  # stable: the lost CAS writes nothing
        assert run.attempt_count == 3
        assert run.parked_reason == "ci_red"

    assert alerts.run_parked_calls == [(project.slug, run.id, "ci_red")]  # never re-fires


# -- scenario 4: the flip -------------------------------------------------------


async def test_flip_on_green_oracle_then_new_run_routes_awaiting_ci(db_session) -> None:
    """A bootstrap project's `awaiting_review` run's PR head goes green ->
    `check_flip` flips the project, applies strict protection exactly
    once, and writes the two `project_events` rows — the run itself stays
    `awaiting_review`, untouched. A second `check_flip` call is a pure
    no-op. Then, routing proof: a brand new run on the now-`oracle_gated`
    project lands `awaiting_ci` on a pushed success, not `awaiting_review`.
    """
    state = RepoState()
    ops = FakeRepoOps(state)
    admin_ops = FakeRepoOps(state)
    alerts = SpyAlertSink()
    project = await seed_project(db_session, lifecycle="bootstrap")
    item = await seed_backlog_item(db_session, project, 1)
    run = await dispatch_to_running(db_session, project, item)

    await finalize_attempt(
        db_session,
        ops,
        run,
        project,
        classification=success_classification(),
        pushed=True,
        quota=NullQuota(),
        alerts=alerts,
    )
    await db_session.commit()
    run = await fresh_run(db_session, run.id)
    assert run.status == "awaiting_review"

    pr = await ops.get_pr(run.pr_number)
    state.oracle_results[pr.head_sha] = CheckState.SUCCESS

    await check_flip(db_session, ops, admin_ops, run, project, alerts=alerts)
    await db_session.commit()

    flipped_project = await fresh_project(db_session, project.id)
    assert flipped_project.lifecycle == "oracle_gated"
    assert admin_ops.apply_strict_protection_calls == ["unattended"]
    assert ops.apply_strict_protection_calls == []  # only the admin view ever applies it

    events = await project_events(db_session, project.id)
    assert {e.event_type for e in events} == {"lifecycle_flipped", "protection_applied"}
    assert alerts.project_flipped_calls == [project.slug]

    run_after = await fresh_run(db_session, run.id)
    assert run_after.status == "awaiting_review"  # the run itself is never touched

    # second check_flip, same green state: fully idempotent.
    await check_flip(db_session, ops, admin_ops, run, project, alerts=alerts)
    await db_session.commit()
    assert admin_ops.apply_strict_protection_calls == ["unattended"]  # still exactly one
    assert len(await project_events(db_session, project.id)) == 2  # still exactly two
    assert alerts.project_flipped_calls == [project.slug]  # still exactly one

    # -- routing proof: a NEW run on the now-oracle_gated project ------------
    item2 = await seed_backlog_item(db_session, flipped_project, 2)
    run2 = await dispatch_to_running(db_session, flipped_project, item2)
    await finalize_attempt(
        db_session,
        ops,
        run2,
        flipped_project,
        classification=success_classification(),
        pushed=True,
        quota=NullQuota(),
        alerts=alerts,
    )
    await db_session.commit()
    run2 = await fresh_run(db_session, run2.id)
    assert run2.status == "awaiting_ci"  # not awaiting_review: the project is oracle_gated now


# -- scenario 5: idempotence sweep (kill -9 equivalence) ----------------------


async def test_idempotence_sweep_kill_minus_9_equivalence_at_each_github_stage(db_session) -> None:
    """Re-runs the bootstrap round trip's three GitHub-interacting stages
    (finalize -> PR-open, accept -> advance_merging, cleanup), each
    redriven the way a crashed-and-restarted process would redrive it, and
    asserts the final state is identical to a single clean run: exactly one
    PR, one merge, one cleanup event, and exactly one `status_changed`
    `run_events` row per real transition (5: queued->claimed,
    claimed->running, running->awaiting_review, awaiting_review->merging,
    merging->merged) — no more, regardless of every redrive above.

    The PR-open crash window is modeled directly rather than by calling
    `finalize_attempt` twice (impossible: the first call's attempt-close
    would leave no open `run_attempts` row for a second call to find): a
    PR is pre-seeded into `RepoOps`'s state for this run's exact head/base,
    standing in for what a crashed-after-PR-creation first attempt would
    have already left on GitHub, and the one real `finalize_attempt` call
    this test makes is the "redrive" — its `open_pr` must land on the
    adopt path, not create a duplicate.
    """
    state = RepoState()
    state.branches["main"] = "main-sha-0"
    tag = uuid.uuid4().hex[:8]
    ops = FakeRepoOps(state)
    admin_ops = FakeRepoOps(state)
    alerts = SpyAlertSink()

    project = await onboard_project(
        db_session, ops, admin_ops, slug=f"e2e-idem-{tag}", owner="acme", repo=f"idem-{tag}"
    )
    await db_session.commit()

    state.ready_issues = [make_ready_issue(42, title="idempotent widget")]
    await sync_backlog(db_session, ops, project)
    assert await intake(db_session, project) == 1
    await db_session.commit()

    run = (await db_session.execute(select(Run).where(Run.project_id == project.id))).scalar_one()
    run = await advance_queued_to_running(db_session, run)

    # -- stage 1: finalize -> PR-open, the crash-window re-drive -------------
    crash_pr_number = 900
    state.prs[crash_pr_number] = _PR(
        number=crash_pr_number,
        head_ref=f"werft/run-{run.id}",
        base_ref=project.unattended_branch,
        head_sha="pre-crash-head-sha",
    )

    await finalize_attempt(
        db_session,
        ops,
        run,
        project,
        classification=success_classification(),
        pushed=True,
        quota=NullQuota(),
        alerts=alerts,
    )
    await db_session.commit()

    run = await fresh_run(db_session, run.id)
    assert run.status == "awaiting_review"
    assert run.pr_number == crash_pr_number  # adopted the pre-existing PR...
    assert len(state.prs) == 1  # ...not a duplicate
    assert len(ops.open_pr_calls) == 1
    assert len(alerts.review_waiting_calls) == 1

    # -- stage 2: operator accept -> advance_merging, redriven on a stale view
    accepted = await transition_run(
        db_session, run_id=run.id, expected_version=run.version, new_status=RunStatus.MERGING
    )
    assert accepted
    await db_session.commit()
    stale_merging = SimpleNamespace(
        id=run.id, version=run.version, pr_number=run.pr_number, branch_name=None
    )

    await advance_merging(db_session, ops, stale_merging, project, alerts=alerts)
    await db_session.commit()
    await advance_merging(db_session, ops, stale_merging, project, alerts=alerts)  # re-drive
    await db_session.commit()

    run = await fresh_run(db_session, run.id)
    assert run.status == "merged"
    assert run.merge_commit_sha is not None
    assert run.version == stale_merging.version + 1  # advanced exactly once, not twice
    assert len(ops.squash_merge_calls) == 1  # the re-drive never re-merges
    assert ops.delete_ref_calls == [f"werft/run-{run.id}"]  # exactly once, from the real merge

    # -- stage 3: cleanup_terminal, redriven on the real, fresh view ----------
    await cleanup_terminal(db_session, ops, run)
    await db_session.commit()
    await cleanup_terminal(db_session, ops, run)  # re-drive: the cleanup event is the guard
    await db_session.commit()

    cleanup_events = await run_events_of_type(db_session, run.id, "cleanup")
    assert len(cleanup_events) == 1
    assert ops.delete_ref_calls == [f"werft/run-{run.id}", f"werft/run-{run.id}"]

    # -- no duplicate run_events beyond the ones this run's real transitions
    #    actually earned, regardless of every redrive above ------------------
    status_changed = await run_events_of_type(db_session, run.id, "status_changed")
    assert len(status_changed) == 5
