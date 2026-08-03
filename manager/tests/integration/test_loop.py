"""`orchestrator/loop.py` against a real DB (SPEC §3.3 items 4-5: short-lived
per-unit handlers, tick-driven correctness; SPEC §6.2 poll cadences; plan
Task 8).

`Orchestrator` is built with a session factory bound to its *own* engine
over the same migrated database `db_session` uses (`orchestrator_session_factory`
below) — the orchestrator owns its own sessions per unit of work (module
docstring), so its tests hand it a real `async_sessionmaker`, not the single
`db_session` fixture instance the rest of the suite seeds/reads through.

Seeding follows `test_ci_watch.py`/`test_merge_flow.py`'s style: raw-SQL
`INSERT`s at whatever status the test needs (the `BEFORE UPDATE` transition
trigger never fires on insert, so any initial status is legal to seed),
then real model instances read back through the ORM.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from werft.config.settings import Settings
from werft.db.models import BacklogItem, Project, Run, RunEvent
from werft.github.client import ConditionalResult, GitHubUnavailable
from werft.github.ops import CheckState, PullRequest
from werft.observe.alerts import NullAlertSink
from werft.orchestrator.finalize import NullQuota
from werft.orchestrator.loop import Orchestrator

# -- fixtures -----------------------------------------------------------------


@pytest.fixture
async def orchestrator_session_factory(migrated_db: str):
    """A real engine bound to the same migrated database `db_session` uses,
    but its own separate connection pool — the orchestrator opens its own
    sessions per unit of work (module docstring), never borrows the test's
    seeding/assertion session. Disposed here so no connection leaks past
    the test."""
    engine = create_async_engine(migrated_db)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def make_orchestrator(session_factory, *, ops_for, admin_ops_for=None) -> Orchestrator:
    return Orchestrator(
        session_factory,
        ops_for,
        admin_ops_for or (lambda project: FakeRepoOps()),
        alerts=NullAlertSink(),
        quota=NullQuota(),
        settings=Settings(tick_seconds=15, issue_poll_seconds=60, check_poll_seconds=30),
    )


# -- fakes / spies ------------------------------------------------------------


class FakeRepoOps:
    """Duck-typed `RepoOps` covering every method a poll/tick sweep might
    call; configurable per test, and records every call made."""

    def __init__(
        self,
        *,
        ready_issues: ConditionalResult | None = None,
        pr: PullRequest | None = None,
        pr_error: Exception | None = None,
        check: CheckState | None = None,
        squash_merge_result: str | None = None,
        squash_merge_error: Exception | None = None,
        close_pr_error: Exception | None = None,
        delete_ref_error: Exception | None = None,
    ) -> None:
        self._ready_issues = (
            ready_issues
            if ready_issues is not None
            else ConditionalResult(modified=False, data=None)
        )
        self._pr = pr
        self._pr_error = pr_error
        self._check = check
        self._squash_merge_result = squash_merge_result
        self._squash_merge_error = squash_merge_error
        self._close_pr_error = close_pr_error
        self._delete_ref_error = delete_ref_error
        self.list_ready_issues_calls = 0
        self.get_pr_calls: list[int] = []
        self.oracle_check_calls: list[str] = []
        self.squash_merge_calls: list[int] = []
        self.close_pr_calls: list[int] = []
        self.delete_ref_calls: list[str] = []
        self.update_branch_calls: list[int] = []
        self.strict_calls: list[str] = []
        self.partial_calls: list[str] = []

    async def list_ready_issues(self) -> ConditionalResult:
        self.list_ready_issues_calls += 1
        return self._ready_issues

    async def get_pr(self, number: int) -> PullRequest | None:
        self.get_pr_calls.append(number)
        if self._pr_error is not None:
            raise self._pr_error
        return self._pr

    async def oracle_check(self, ref: str) -> CheckState:
        self.oracle_check_calls.append(ref)
        assert self._check is not None
        return self._check

    async def update_branch(self, number: int, expected_head_sha: str) -> None:
        self.update_branch_calls.append(number)

    async def squash_merge(self, number: int, head_sha: str, commit_title: str) -> str:
        self.squash_merge_calls.append(number)
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

    async def apply_strict_protection(self, branch: str) -> None:
        self.strict_calls.append(branch)

    async def apply_partial_protection(self, branch: str) -> None:
        self.partial_calls.append(branch)


class FailingForProjectOpsFactory:
    """An `ops_for` factory that raises `GitHubUnavailable` for one named
    project id and returns a working `FakeRepoOps` for every other project
    — the loop-resilience fixture (SPEC §3.3: one project's GitHub outage
    never starves the rest). Targeting a specific project id (rather than
    "whichever comes first") keeps the test independent of the discovery
    query's row order, which is unordered by design."""

    def __init__(self, failing_project_id: uuid.UUID, **fake_kwargs) -> None:
        self.calls: list[uuid.UUID] = []
        self._failing_project_id = failing_project_id
        self._fake_kwargs = fake_kwargs

    def __call__(self, project: Project) -> FakeRepoOps:
        self.calls.append(project.id)
        if project.id == self._failing_project_id:
            raise GitHubUnavailable(503, "simulated outage")
        return FakeRepoOps(**self._fake_kwargs)


class RaisingForOnePrNumberOps:
    """A `RepoOps` whose `get_pr` raises an arbitrary (non-GitHub)
    exception for one specific PR number and behaves normally (scripted PR
    + oracle check) for every other. Two runs sharing this *one* ops
    instance (same project) proves per-*run* isolation within a single
    sweep — distinct from `FailingForProjectOpsFactory`'s per-*project*
    isolation above — and that "any exception", not only
    `GitHubUnavailable`, is contained (SPEC §3.3)."""

    def __init__(
        self,
        *,
        raise_for_pr: int,
        raise_error: Exception,
        prs: dict[int, PullRequest],
        checks: dict[str, CheckState],
    ) -> None:
        self._raise_for_pr = raise_for_pr
        self._raise_error = raise_error
        self._prs = prs
        self._checks = checks
        self.get_pr_calls: list[int] = []
        self.oracle_check_calls: list[str] = []

    async def get_pr(self, number: int) -> PullRequest | None:
        self.get_pr_calls.append(number)
        if number == self._raise_for_pr:
            raise self._raise_error
        return self._prs[number]

    async def oracle_check(self, ref: str) -> CheckState:
        self.oracle_check_calls.append(ref)
        return self._checks[ref]


def make_pr(
    number: int, *, head_sha: str = "deadbeef", mergeable: bool | None = True
) -> PullRequest:
    return PullRequest(
        number=number,
        state="open",
        merged=False,
        head_ref=f"werft/run-{number}",
        head_sha=head_sha,
        base_ref="unattended",
        mergeable=mergeable,
        mergeable_state="clean",
        html_url=f"https://github.test/o/r/pull/{number}",
    )


# -- seeding -------------------------------------------------------------------


async def seed_project(
    session, *, is_paused: bool = False, lifecycle: str = "oracle_gated"
) -> Project:
    tag = uuid.uuid4().hex[:8]
    pid = (
        await session.execute(
            text(
                "INSERT INTO projects (slug, github_owner, github_repo, is_paused, lifecycle) "
                "VALUES (:slug, 'o', :repo, :paused, :lifecycle) RETURNING id"
            ),
            {"slug": f"p-{tag}", "repo": f"r-{tag}", "paused": is_paused, "lifecycle": lifecycle},
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
    status: str,
    pr_number: int | None = None,
    next_attempt_at: datetime | None = None,
) -> Run:
    rid = (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status, provider, pr_number, "
                "next_attempt_at) "
                "VALUES (:p, :b, :status, 'claude', :pr, COALESCE(:nat, now())) RETURNING id"
            ),
            {
                "p": project.id,
                "b": item.id,
                "status": status,
                "pr": pr_number,
                "nat": next_attempt_at,
            },
        )
    ).scalar_one()
    await session.commit()
    return await session.get(Run, rid)


async def seed_open_attempt(session, run_id: uuid.UUID, *, attempt_no: int = 1) -> None:
    """Mirrors what `finalize_attempt`'s pushed-success path leaves behind:
    a closed-out (`ended_at` set) attempt row with `outcome` still `NULL`
    — "pending oracle" — which `advance_awaiting_ci`'s green path fills in.
    Without this row, `advance_awaiting_ci` raises `NoResultFound`."""
    await session.execute(
        text(
            "INSERT INTO run_attempts (run_id, attempt_no, provider, started_at, ended_at) "
            "VALUES (:r, :n, 'claude', now() - interval '30 seconds', now())"
        ),
        {"r": run_id, "n": attempt_no},
    )
    await session.commit()


async def fresh_run(session, run_id) -> Run:
    return await session.get(Run, run_id, populate_existing=True)


async def fresh_project(session, project_id) -> Project:
    return await session.get(Project, project_id, populate_existing=True)


async def cleanup_events(session, run_id) -> list[RunEvent]:
    result = await session.execute(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.event_type == "cleanup")
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def runs_for(session, project_id) -> list[Run]:
    result = await session.execute(
        select(Run).where(Run.project_id == project_id).execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


# -- tick_once: blocked_quota wake ----------------------------------------------


async def test_tick_wakes_blocked_quota_run_whose_next_attempt_at_is_in_the_past(
    db_session, orchestrator_session_factory
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(
        db_session,
        project,
        item,
        status="blocked_quota",
        next_attempt_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    orchestrator = make_orchestrator(orchestrator_session_factory, ops_for=lambda p: FakeRepoOps())
    await orchestrator.tick_once()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "queued"


async def test_tick_leaves_blocked_quota_run_whose_next_attempt_at_is_in_the_future(
    db_session, orchestrator_session_factory
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(
        db_session,
        project,
        item,
        status="blocked_quota",
        next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
    )

    orchestrator = make_orchestrator(orchestrator_session_factory, ops_for=lambda p: FakeRepoOps())
    await orchestrator.tick_once()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "blocked_quota"
    assert updated.version == run.version  # zero writes


# -- tick_once: terminal cleanup -------------------------------------------------


async def test_tick_cleans_up_a_canceled_run_with_an_open_pr(
    db_session, orchestrator_session_factory
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="canceled", pr_number=101)

    ops = FakeRepoOps()
    orchestrator = make_orchestrator(orchestrator_session_factory, ops_for=lambda p: ops)
    await orchestrator.tick_once()

    assert ops.close_pr_calls == [101]
    assert ops.delete_ref_calls == [f"werft/run-{run.id}"]
    assert len(await cleanup_events(db_session, run.id)) == 1


async def test_tick_terminal_cleanup_is_a_no_op_once_the_cleanup_event_exists(
    db_session, orchestrator_session_factory
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="canceled", pr_number=101)

    ops = FakeRepoOps()
    orchestrator = make_orchestrator(orchestrator_session_factory, ops_for=lambda p: ops)
    await orchestrator.tick_once()
    await orchestrator.tick_once()

    assert ops.close_pr_calls == [101]  # still exactly one
    assert len(await cleanup_events(db_session, run.id)) == 1


# -- tick_once: merging advance --------------------------------------------------


async def test_tick_advances_a_due_merging_run(db_session, orchestrator_session_factory) -> None:
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="merging", pr_number=101)

    ops = FakeRepoOps(pr=make_pr(101, mergeable=True), squash_merge_result="feedbeef")
    orchestrator = make_orchestrator(orchestrator_session_factory, ops_for=lambda p: ops)
    await orchestrator.tick_once()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "merged"
    assert ops.squash_merge_calls == [101]


# -- poll_checks_once: awaiting_ci / awaiting_review(bootstrap) / merging --------


async def test_poll_checks_once_advances_awaiting_ci_check_flip_and_merging(
    db_session, orchestrator_session_factory
) -> None:
    oracle_project = await seed_project(db_session, lifecycle="oracle_gated")
    oracle_item = await seed_backlog_item(db_session, oracle_project, 1)
    ci_run = await seed_run(
        db_session, oracle_project, oracle_item, status="awaiting_ci", pr_number=201
    )
    await seed_open_attempt(db_session, ci_run.id)

    bootstrap_project = await seed_project(db_session, lifecycle="bootstrap")
    bootstrap_item = await seed_backlog_item(db_session, bootstrap_project, 1)
    review_run = await seed_run(
        db_session, bootstrap_project, bootstrap_item, status="awaiting_review", pr_number=202
    )

    merging_project = await seed_project(db_session, lifecycle="oracle_gated")
    merging_item = await seed_backlog_item(db_session, merging_project, 1)
    merging_run = await seed_run(
        db_session, merging_project, merging_item, status="merging", pr_number=203
    )

    ci_ops = FakeRepoOps(pr=make_pr(201, head_sha="cafef00d"), check=CheckState.SUCCESS)
    review_ops = FakeRepoOps(pr=make_pr(202, head_sha="feedface"), check=CheckState.SUCCESS)
    merging_ops = FakeRepoOps(pr=make_pr(203, mergeable=True), squash_merge_result="deadc0de")
    shared_admin_ops = FakeRepoOps()

    ops_by_project = {
        oracle_project.id: ci_ops,
        bootstrap_project.id: review_ops,
        merging_project.id: merging_ops,
    }

    orchestrator = make_orchestrator(
        orchestrator_session_factory,
        ops_for=lambda project: ops_by_project[project.id],
        admin_ops_for=lambda project: shared_admin_ops,
    )
    await orchestrator.poll_checks_once()

    updated_ci_run = await fresh_run(db_session, ci_run.id)
    assert updated_ci_run.status == "merging"  # green oracle -> merging
    assert ci_ops.oracle_check_calls == ["cafef00d"]

    updated_bootstrap_project = await fresh_project(db_session, bootstrap_project.id)
    assert updated_bootstrap_project.lifecycle == "oracle_gated"  # the flip fired
    assert shared_admin_ops.strict_calls == ["unattended"]
    updated_review_run = await fresh_run(db_session, review_run.id)
    assert updated_review_run.status == "awaiting_review"  # the run itself is never touched

    updated_merging_run = await fresh_run(db_session, merging_run.id)
    assert updated_merging_run.status == "merged"


async def test_poll_checks_once_never_calls_check_flip_for_an_oracle_gated_projects_review_run(
    db_session, orchestrator_session_factory
) -> None:
    """Decision 4's guard, asserted directly: an `oracle_gated` project's
    `awaiting_review` run must never reach `check_flip` at all — the
    poller's own query is the guard, not `check_flip` itself."""
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item = await seed_backlog_item(db_session, project, 1)
    await seed_run(db_session, project, item, status="awaiting_review", pr_number=301)

    ops = FakeRepoOps()  # oracle_check would assert-fail if it were ever called
    orchestrator = make_orchestrator(orchestrator_session_factory, ops_for=lambda p: ops)
    await orchestrator.poll_checks_once()

    assert ops.get_pr_calls == []
    assert ops.oracle_check_calls == []


async def test_poll_checks_once_isolates_one_runs_handler_exception_from_the_rest(
    db_session, orchestrator_session_factory
) -> None:
    """Run-level isolation, distinct from the project-level isolation
    proven below: two `awaiting_ci` runs in the *same* project share one
    `RepoOps` instance, and the first run's `get_pr` raises a plain
    `RuntimeError` — not even a `GitHubUnavailable` — proving that *any*
    exception from one run's handler must not skip the remaining runs
    (SPEC §3.3)."""
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item1 = await seed_backlog_item(db_session, project, 1)
    item2 = await seed_backlog_item(db_session, project, 2)

    broken_run = await seed_run(db_session, project, item1, status="awaiting_ci", pr_number=901)
    await seed_open_attempt(db_session, broken_run.id)
    healthy_run = await seed_run(db_session, project, item2, status="awaiting_ci", pr_number=902)
    await seed_open_attempt(db_session, healthy_run.id)

    ops = RaisingForOnePrNumberOps(
        raise_for_pr=901,
        raise_error=RuntimeError("boom - not even a GitHub error"),
        prs={902: make_pr(902, head_sha="feedbead")},
        checks={"feedbead": CheckState.SUCCESS},
    )
    orchestrator = make_orchestrator(orchestrator_session_factory, ops_for=lambda p: ops)

    await orchestrator.poll_checks_once()  # must not raise

    assert set(ops.get_pr_calls) == {901, 902}

    updated_broken = await fresh_run(db_session, broken_run.id)
    assert updated_broken.status == "awaiting_ci"  # untouched by the failed unit

    updated_healthy = await fresh_run(db_session, healthy_run.id)
    assert updated_healthy.status == "merging"  # still advanced despite the sibling's crash


# -- poll_issues_once: paused projects are skipped -------------------------------


async def test_poll_issues_once_skips_paused_projects(
    db_session, orchestrator_session_factory
) -> None:
    project = await seed_project(db_session, is_paused=True)
    await seed_backlog_item(db_session, project, 1)

    ops = FakeRepoOps()
    orchestrator = make_orchestrator(orchestrator_session_factory, ops_for=lambda p: ops)
    await orchestrator.poll_issues_once()

    assert ops.list_ready_issues_calls == 0


# -- loop resilience: one project's outage never starves the rest ---------------


async def test_poll_issues_once_isolates_one_projects_github_outage_from_the_rest(
    db_session, orchestrator_session_factory
) -> None:
    project_a = await seed_project(db_session)
    await seed_backlog_item(db_session, project_a, 1)
    project_b = await seed_project(db_session)
    await seed_backlog_item(db_session, project_b, 2)

    factory = FailingForProjectOpsFactory(
        project_a.id,
        ready_issues=ConditionalResult(
            modified=True,
            data=[{"number": 2, "title": "t", "labels": [], "updated_at": "2026-08-01T00:00:00Z"}],
        ),
    )
    orchestrator = make_orchestrator(orchestrator_session_factory, ops_for=factory)

    await orchestrator.poll_issues_once()  # must not raise despite project_a's outage

    assert len(factory.calls) == 2  # both projects were attempted this poll

    runs_b = await runs_for(db_session, project_b.id)
    assert len(runs_b) == 1
    assert runs_b[0].status == "queued"

    assert await runs_for(db_session, project_a.id) == []  # never got past the outage


# -- run: three loops, stop-responsive -------------------------------------------


async def test_run_executes_loops_and_stops_promptly_after_the_stop_event(
    orchestrator_session_factory,
) -> None:
    settings = Settings(tick_seconds=1, issue_poll_seconds=1, check_poll_seconds=1)
    orchestrator = Orchestrator(
        orchestrator_session_factory,
        lambda project: FakeRepoOps(),
        lambda project: FakeRepoOps(),
        alerts=NullAlertSink(),
        quota=NullQuota(),
        settings=settings,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(orchestrator.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
