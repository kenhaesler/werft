"""`orchestrator/ci_watch.py` against a real DB (SPEC §3.2 edges; SPEC §6.2
mechanics; SPEC §3.1 the flip; plan Behavioral decisions 2-4).

Seeding follows `test_backlog.py`/`test_finalize.py`'s style: a raw-SQL
`queued` insert, then raw-SQL `UPDATE`s (which still fire the transition
trigger, same as any ORM update) drive the row through the legal
`claimed -> running` hops, and `transition_run` drives the final
`running -> awaiting_ci`/`awaiting_review` hop — that last hop is the one
whose `status_changed` `run_events` row `ci_watch.py`'s `ci_timeout` epoch
(decision 2) actually reads.
"""

import uuid
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select, text

from werft.db.models import BacklogItem, Project, ProjectEvent, Run, RunAttempt, RunEvent
from werft.db.transitions import transition_run
from werft.domain.projects import ProjectLifecycle
from werft.domain.runs import RunStatus
from werft.github.client import GitHubUnavailable
from werft.github.ops import CheckState, PullRequest
from werft.orchestrator.ci_watch import advance_awaiting_ci, check_flip, flip_project

# -- fakes / spies ------------------------------------------------------------


class FakeRepoOps:
    """Duck-typed `RepoOps.get_pr`/`.oracle_check`: each returns one canned
    value (or raises one canned exception) on every call, and records every
    call made — enough for a single-poll decision, which is all
    `advance_awaiting_ci`/`check_flip` ever make per invocation."""

    def __init__(
        self,
        *,
        pr: PullRequest | None = None,
        pr_error: Exception | None = None,
        check: CheckState | None = None,
        check_error: Exception | None = None,
    ) -> None:
        self._pr = pr
        self._pr_error = pr_error
        self._check = check
        self._check_error = check_error
        self.get_pr_calls: list[int] = []
        self.oracle_check_calls: list[str] = []

    async def get_pr(self, number: int) -> PullRequest | None:
        self.get_pr_calls.append(number)
        if self._pr_error is not None:
            raise self._pr_error
        return self._pr

    async def oracle_check(self, ref: str) -> CheckState:
        self.oracle_check_calls.append(ref)
        if self._check_error is not None:
            raise self._check_error
        assert self._check is not None
        return self._check


class FakeAdminOps:
    """Duck-typed `RepoOps` protection calls only — `flip_project`'s other
    seam. Records every call so a test can assert exactly-once."""

    def __init__(self) -> None:
        self.strict_calls: list[str] = []
        self.partial_calls: list[str] = []

    async def apply_strict_protection(self, branch: str) -> None:
        self.strict_calls.append(branch)

    async def apply_partial_protection(self, branch: str) -> None:
        self.partial_calls.append(branch)


class SpyAlertSink:
    def __init__(self) -> None:
        self.run_parked_calls: list[tuple[str, uuid.UUID, str]] = []
        self.project_flipped_calls: list[str] = []
        self.review_waiting_calls: list[tuple[str, uuid.UUID, str]] = []
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
    number: int, *, head_sha: str = "deadbeef", state: str = "open", merged: bool = False
) -> PullRequest:
    return PullRequest(
        number=number,
        state=state,
        merged=merged,
        head_ref=f"werft/run-{number}",
        head_sha=head_sha,
        base_ref="unattended",
        mergeable=None,
        mergeable_state="unknown",
        html_url=f"https://github.test/o/r/pull/{number}",
    )


# -- seeding -------------------------------------------------------------------


async def seed_project(
    session,
    *,
    lifecycle: str = "oracle_gated",
    ci_timeout_seconds: int = 21600,
    unattended_branch: str = "unattended",
) -> Project:
    tag = uuid.uuid4().hex[:8]
    pid = (
        await session.execute(
            text(
                "INSERT INTO projects (slug, github_owner, github_repo, lifecycle, "
                "unattended_branch, ci_timeout_seconds) "
                "VALUES (:slug, 'o', :repo, :lifecycle, :branch, :timeout) RETURNING id"
            ),
            {
                "slug": f"p-{tag}",
                "repo": f"r-{tag}",
                "lifecycle": lifecycle,
                "branch": unattended_branch,
                "timeout": ci_timeout_seconds,
            },
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


async def seed_queued_run(
    session, project: Project, item: BacklogItem, *, attempt_count: int = 0, max_attempts: int = 3
) -> uuid.UUID:
    rid = (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status, provider, "
                "attempt_count, max_attempts) "
                "VALUES (:p, :b, 'queued', 'claude', :ac, :ma) RETURNING id"
            ),
            {"p": project.id, "b": item.id, "ac": attempt_count, "ma": max_attempts},
        )
    ).scalar_one()
    await session.commit()
    return rid


async def advance_raw(session, run_id: uuid.UUID, status: str) -> None:
    """Raw `UPDATE` — still fires the transition trigger (legality check +
    `status_changed` event) the same as an ORM update would, but skips
    `version` bookkeeping: fine for the intermediate `claimed`/`running`
    hops nothing here CASes against."""
    await session.execute(
        text("UPDATE runs SET status = :s WHERE id = :id"), {"s": status, "id": run_id}
    )
    await session.commit()


async def seed_open_attempt(session, run_id: uuid.UUID, *, attempt_no: int = 1) -> None:
    """Mirrors what `finalize_attempt`'s pushed-success path leaves behind:
    a closed-out (`ended_at` set) attempt row with `outcome` still `NULL`
    — "pending oracle"."""
    await session.execute(
        text(
            "INSERT INTO run_attempts (run_id, attempt_no, provider, started_at, ended_at) "
            "VALUES (:r, :n, 'claude', now() - interval '30 seconds', now())"
        ),
        {"r": run_id, "n": attempt_no},
    )
    await session.commit()


async def seed_awaiting_ci(
    session,
    project: Project,
    item: BacklogItem,
    *,
    attempt_count: int = 0,
    max_attempts: int = 3,
    pr_number: int = 101,
) -> Run:
    rid = await seed_queued_run(
        session, project, item, attempt_count=attempt_count, max_attempts=max_attempts
    )
    await advance_raw(session, rid, "claimed")
    await advance_raw(session, rid, "running")
    await seed_open_attempt(session, rid)
    run = await session.get(Run, rid)
    ok = await transition_run(
        session,
        run_id=rid,
        expected_version=run.version,
        new_status=RunStatus.AWAITING_CI,
        extra={"pr_number": pr_number},
    )
    assert ok
    await session.commit()
    return await session.get(Run, rid, populate_existing=True)


async def seed_awaiting_review(
    session, project: Project, item: BacklogItem, *, pr_number: int = 202
) -> Run:
    rid = await seed_queued_run(session, project, item)
    await advance_raw(session, rid, "claimed")
    await advance_raw(session, rid, "running")
    run = await session.get(Run, rid)
    ok = await transition_run(
        session,
        run_id=rid,
        expected_version=run.version,
        new_status=RunStatus.AWAITING_REVIEW,
        extra={"pr_number": pr_number},
    )
    assert ok
    await session.commit()
    return await session.get(Run, rid, populate_existing=True)


async def backdate_awaiting_ci_event(session, run_id: uuid.UUID, *, seconds_ago: int) -> None:
    await session.execute(
        text(
            "UPDATE run_events SET created_at = now() - make_interval(secs => :offset) "
            "WHERE run_id = :run_id AND event_type = 'status_changed' "
            "AND payload ->> 'to' = 'awaiting_ci'"
        ),
        {"run_id": run_id, "offset": seconds_ago},
    )
    await session.commit()


async def fresh_run(session, run_id) -> Run:
    return await session.get(Run, run_id, populate_existing=True)


async def fresh_project(session, project_id) -> Project:
    return await session.get(Project, project_id, populate_existing=True)


async def latest_attempt(session, run_id) -> RunAttempt:
    result = await session.execute(
        select(RunAttempt)
        .where(RunAttempt.run_id == run_id)
        .order_by(RunAttempt.attempt_no.desc())
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


async def ci_observed_events(session, run_id) -> list[RunEvent]:
    result = await session.execute(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.event_type == "ci_observed")
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def project_events(session, project_id) -> list[ProjectEvent]:
    result = await session.execute(
        select(ProjectEvent)
        .where(ProjectEvent.project_id == project_id)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


# -- advance_awaiting_ci: green -------------------------------------------------


async def test_green_oracle_advances_to_merging_fills_ci_green_and_records_ci_observed(
    db_session,
) -> None:
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_awaiting_ci(db_session, project, item, pr_number=101)
    ops = FakeRepoOps(pr=make_pr(101, head_sha="cafef00d"), check=CheckState.SUCCESS)
    alerts = SpyAlertSink()

    await advance_awaiting_ci(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "merging"

    attempt = await latest_attempt(db_session, run.id)
    assert attempt.outcome == "ci_green"

    events = await ci_observed_events(db_session, run.id)
    assert len(events) == 1
    assert events[0].payload == {"sha": "cafef00d", "conclusion": "success"}
    assert alerts.run_parked_calls == []


# -- advance_awaiting_ci: red ----------------------------------------------------


async def test_red_oracle_with_budget_left_requeues_and_fills_ci_red(db_session) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_awaiting_ci(db_session, project, item, attempt_count=0, max_attempts=3)
    ops = FakeRepoOps(pr=make_pr(101), check=CheckState.FAILURE)
    alerts = SpyAlertSink()

    await advance_awaiting_ci(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "queued"
    assert updated.attempt_count == 1

    attempt = await latest_attempt(db_session, run.id)
    assert attempt.outcome == "ci_red"
    assert len(await ci_observed_events(db_session, run.id)) == 1
    assert alerts.run_parked_calls == []  # budget left: never parks, never alerts


async def test_red_oracle_at_attempt_budget_parks_ci_red_and_alerts(db_session) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_awaiting_ci(db_session, project, item, attempt_count=2, max_attempts=3)
    ops = FakeRepoOps(pr=make_pr(101), check=CheckState.FAILURE)
    alerts = SpyAlertSink()

    await advance_awaiting_ci(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "parked"
    assert updated.attempt_count == 3
    assert updated.parked_reason == "ci_red"
    assert alerts.run_parked_calls == [(project.slug, run.id, "ci_red")]


# -- advance_awaiting_ci: pending / absent / ci_timeout --------------------------


async def test_pending_with_young_wait_is_a_no_op_with_zero_writes(db_session) -> None:
    project = await seed_project(db_session, ci_timeout_seconds=21600)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_awaiting_ci(db_session, project, item)
    ops = FakeRepoOps(pr=make_pr(101), check=CheckState.PENDING)
    alerts = SpyAlertSink()

    before_events = await ci_observed_events(db_session, run.id)

    await advance_awaiting_ci(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "awaiting_ci"
    assert updated.version == run.version  # zero writes: version never bumped

    attempt = await latest_attempt(db_session, run.id)
    assert attempt.outcome is None  # still pending oracle
    assert await ci_observed_events(db_session, run.id) == before_events
    assert alerts.run_parked_calls == []


async def test_pending_older_than_ci_timeout_parks_ci_timeout_and_alerts(db_session) -> None:
    project = await seed_project(db_session, ci_timeout_seconds=3600)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_awaiting_ci(db_session, project, item)
    await backdate_awaiting_ci_event(db_session, run.id, seconds_ago=3601)
    ops = FakeRepoOps(pr=make_pr(101), check=CheckState.PENDING)
    alerts = SpyAlertSink()

    await advance_awaiting_ci(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "parked"
    assert updated.parked_reason == "ci_timeout"
    assert alerts.run_parked_calls == [(project.slug, run.id, "ci_timeout")]
    # a timeout is never a verdict — no attempt outcome, no ci_observed row
    attempt = await latest_attempt(db_session, run.id)
    assert attempt.outcome is None
    assert await ci_observed_events(db_session, run.id) == []


async def test_absent_older_than_ci_timeout_also_parks_ci_timeout(db_session) -> None:
    """Design constraint: the timeout check applies on both `PENDING` and
    `ABSENT` — an oracle that never even started is exactly as stuck as one
    still running."""
    project = await seed_project(db_session, ci_timeout_seconds=3600)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_awaiting_ci(db_session, project, item)
    await backdate_awaiting_ci_event(db_session, run.id, seconds_ago=3601)
    ops = FakeRepoOps(pr=make_pr(101), check=CheckState.ABSENT)
    alerts = SpyAlertSink()

    await advance_awaiting_ci(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "parked"
    assert updated.parked_reason == "ci_timeout"


# -- advance_awaiting_ci: PR gone -------------------------------------------------


async def test_pr_404_advances_to_failed(db_session) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_awaiting_ci(db_session, project, item, pr_number=101)
    ops = FakeRepoOps(pr=None)
    alerts = SpyAlertSink()

    await advance_awaiting_ci(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "failed"
    assert updated.error_message
    assert ops.oracle_check_calls == []  # never reached: no PR, no check to make


async def test_pr_closed_not_merged_advances_to_failed(db_session) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_awaiting_ci(db_session, project, item, pr_number=101)
    ops = FakeRepoOps(pr=make_pr(101, state="closed", merged=False))
    alerts = SpyAlertSink()

    await advance_awaiting_ci(db_session, ops, run, project, alerts=alerts)
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "failed"


# -- advance_awaiting_ci: GitHubUnavailable containment ---------------------------


async def test_get_pr_unavailable_leaves_state_untouched_and_does_not_raise(db_session) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_awaiting_ci(db_session, project, item, pr_number=101)
    ops = FakeRepoOps(pr_error=GitHubUnavailable(503, "service unavailable"))
    alerts = SpyAlertSink()

    await advance_awaiting_ci(db_session, ops, run, project, alerts=alerts)  # must not raise
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "awaiting_ci"
    assert updated.version == run.version


async def test_oracle_check_unavailable_leaves_state_untouched_and_does_not_raise(
    db_session,
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_awaiting_ci(db_session, project, item, pr_number=101)
    ops = FakeRepoOps(pr=make_pr(101), check_error=GitHubUnavailable(500, "boom"))
    alerts = SpyAlertSink()

    await advance_awaiting_ci(db_session, ops, run, project, alerts=alerts)  # must not raise
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "awaiting_ci"
    assert updated.version == run.version


# -- advance_awaiting_ci: lost CAS races a concurrent operator cancel ------------


async def test_red_oracle_racing_an_out_of_band_cancel_returns_cleanly_without_raising(
    db_session,
) -> None:
    """`awaiting_ci -> canceled` is itself a legal, routine edge (an
    operator can cancel a run while CI is still being evaluated) — this
    seeds a run to `awaiting_ci`, captures a stale view of it, then
    transitions the *real* row to `canceled` out from under it (as a
    concurrent operator cancel would), before calling `advance_awaiting_ci`
    with the stale view. The `awaiting_ci -> queued` CAS must lose (stale
    version), and the lost-CAS handler must recognize the race and return
    cleanly rather than raising — the canceled status stands, and no
    `run_parked` alert fires for a park that never actually happened.

    The stale view is deliberately a bare `SimpleNamespace`, not the `Run`
    instance itself: SQLAlchemy's ORM-enabled `UPDATE` auto-synchronizes
    already-loaded, identity-mapped objects matching its `WHERE` clause, so
    reusing the same `Run` object across both calls would silently pick up
    the out-of-band transition's version bump — masking exactly the
    staleness this test exists to exercise (same technique as
    `test_finalize.py`'s equivalent race test).
    """
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_awaiting_ci(db_session, project, item, attempt_count=0, max_attempts=3)
    stale_view = SimpleNamespace(
        id=run.id,
        version=run.version,
        pr_number=run.pr_number,
        attempt_count=run.attempt_count,
        max_attempts=run.max_attempts,
    )

    canceled = await transition_run(
        db_session, run_id=run.id, expected_version=run.version, new_status=RunStatus.CANCELED
    )
    assert canceled  # sanity: awaiting_ci -> canceled really is a legal edge
    await db_session.commit()

    ops = FakeRepoOps(pr=make_pr(101), check=CheckState.FAILURE)
    alerts = SpyAlertSink()

    await advance_awaiting_ci(db_session, ops, stale_view, project, alerts=alerts)  # must not raise
    await db_session.commit()

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "canceled"  # the race's outcome stands, untouched
    assert alerts.run_parked_calls == []  # no park ever actually happened

    # the CI observation itself is a true fact independent of the run's
    # ultimate (raced) fate, so it is still recorded — same principle as
    # finalize.py's unconditional attempt-close-before-CAS.
    attempt = await latest_attempt(db_session, run.id)
    assert attempt.outcome == "ci_red"
    assert len(await ci_observed_events(db_session, run.id)) == 1


# -- flip_project ---------------------------------------------------------------


async def test_flip_project_upgrade_applies_strict_protection_writes_events_and_alerts(
    db_session,
) -> None:
    project = await seed_project(db_session, lifecycle="bootstrap", unattended_branch="unattended")
    admin_ops = FakeAdminOps()
    alerts = SpyAlertSink()

    flipped = await flip_project(
        db_session, admin_ops, project, to=ProjectLifecycle.ORACLE_GATED, alerts=alerts
    )
    await db_session.commit()

    assert flipped is True
    assert admin_ops.strict_calls == ["unattended"]
    assert admin_ops.partial_calls == []

    updated = await fresh_project(db_session, project.id)
    assert updated.lifecycle == "oracle_gated"

    events = await project_events(db_session, project.id)
    types = {e.event_type for e in events}
    assert types == {"lifecycle_flipped", "protection_applied"}
    assert alerts.project_flipped_calls == [project.slug]


async def test_flip_project_downgrade_applies_partial_protection_and_never_alerts(
    db_session,
) -> None:
    project = await seed_project(db_session, lifecycle="oracle_gated")
    admin_ops = FakeAdminOps()
    alerts = SpyAlertSink()

    flipped = await flip_project(
        db_session, admin_ops, project, to=ProjectLifecycle.BOOTSTRAP, alerts=alerts
    )
    await db_session.commit()

    assert flipped is True
    assert admin_ops.partial_calls == ["unattended"]
    assert admin_ops.strict_calls == []

    updated = await fresh_project(db_session, project.id)
    assert updated.lifecycle == "bootstrap"
    assert alerts.project_flipped_calls == []  # downgrade is repair, not the milestone


async def test_flip_project_guard_miss_returns_false_with_zero_side_effects(db_session) -> None:
    """The project is already `oracle_gated` — a stale or duplicate caller
    asking to flip it *to* `oracle_gated` finds the guard's `from` (=
    `bootstrap`) doesn't match, and must do nothing at all."""
    project = await seed_project(db_session, lifecycle="oracle_gated")
    admin_ops = FakeAdminOps()
    alerts = SpyAlertSink()

    flipped = await flip_project(
        db_session, admin_ops, project, to=ProjectLifecycle.ORACLE_GATED, alerts=alerts
    )
    await db_session.commit()

    assert flipped is False
    assert admin_ops.strict_calls == []
    assert admin_ops.partial_calls == []
    assert await project_events(db_session, project.id) == []
    assert alerts.project_flipped_calls == []

    updated = await fresh_project(db_session, project.id)
    assert updated.lifecycle == "oracle_gated"


# -- check_flip: the flip's observation site -------------------------------------


async def test_check_flip_green_on_bootstrap_review_head_flips_project_run_stays_awaiting_review(
    db_session,
) -> None:
    project = await seed_project(db_session, lifecycle="bootstrap")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_awaiting_review(db_session, project, item, pr_number=202)
    ops = FakeRepoOps(pr=make_pr(202, head_sha="feedface"), check=CheckState.SUCCESS)
    admin_ops = FakeAdminOps()
    alerts = SpyAlertSink()

    await check_flip(db_session, ops, admin_ops, run, project, alerts=alerts)
    await db_session.commit()

    updated_project = await fresh_project(db_session, project.id)
    assert updated_project.lifecycle == "oracle_gated"
    assert admin_ops.strict_calls == ["unattended"]

    events = await project_events(db_session, project.id)
    assert {e.event_type for e in events} == {"lifecycle_flipped", "protection_applied"}
    assert alerts.project_flipped_calls == [project.slug]

    updated_run = await fresh_run(db_session, run.id)
    assert updated_run.status == "awaiting_review"  # the run itself is never touched

    # second call, same green state: fully idempotent — no second protection
    # call, no second event, no second alert.
    await check_flip(db_session, ops, admin_ops, run, project, alerts=alerts)
    await db_session.commit()

    assert admin_ops.strict_calls == ["unattended"]  # still exactly one
    assert len(await project_events(db_session, project.id)) == 2  # still exactly two
    assert alerts.project_flipped_calls == [project.slug]  # still exactly one


async def test_check_flip_pending_is_a_no_op(db_session) -> None:
    project = await seed_project(db_session, lifecycle="bootstrap")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_awaiting_review(db_session, project, item, pr_number=202)
    ops = FakeRepoOps(pr=make_pr(202), check=CheckState.PENDING)
    admin_ops = FakeAdminOps()
    alerts = SpyAlertSink()

    await check_flip(db_session, ops, admin_ops, run, project, alerts=alerts)
    await db_session.commit()

    updated_project = await fresh_project(db_session, project.id)
    assert updated_project.lifecycle == "bootstrap"
    assert admin_ops.strict_calls == []
    assert await project_events(db_session, project.id) == []


async def test_check_flip_get_pr_unavailable_leaves_state_untouched_and_does_not_raise(
    db_session,
) -> None:
    project = await seed_project(db_session, lifecycle="bootstrap")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_awaiting_review(db_session, project, item, pr_number=202)
    ops = FakeRepoOps(pr_error=GitHubUnavailable(503, "service unavailable"))
    admin_ops = FakeAdminOps()
    alerts = SpyAlertSink()

    await check_flip(db_session, ops, admin_ops, run, project, alerts=alerts)  # must not raise
    await db_session.commit()

    updated_project = await fresh_project(db_session, project.id)
    assert updated_project.lifecycle == "bootstrap"
    assert admin_ops.strict_calls == []
