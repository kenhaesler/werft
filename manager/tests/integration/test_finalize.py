"""`orchestrator/finalize.py` against a real DB (SPEC §3.2; plan Behavioral
decisions 8 and 9).

Seeding follows `test_backlog.py`/`test_triggers.py`'s style: raw SQL
inserts (a `running` run with one open, un-finalized `run_attempts` row is
not reachable through any legal ORM-level transition path, so it is staged
directly), then real model instances read back through the ORM so the
functions under test see genuine rows.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from werft.contracts.result import ResultStatus
from werft.db.models import BacklogItem, Project, Run, RunAttempt
from werft.db.transitions import transition_run
from werft.domain.attempts import AttemptOutcome
from werft.domain.runs import RunStatus
from werft.github.ops import PullRequest
from werft.orchestrator.finalize import advance_failed, finalize_attempt, open_pr_and_wait
from werft.providers.base import Classification

# -- fakes / spies ----------------------------------------------------------


class FakeRepoOps:
    """Duck-typed `RepoOps.open_pr`. Returns one canned `PullRequest`
    forever once its short list is exhausted — exactly what a real
    adopt-on-422 does on a re-driven call (the second `open_pr` resolves to
    the *same* existing PR, not a new one). Records every call so a test can
    assert how many PR-create/adopt round trips actually happened."""

    def __init__(self, *prs: PullRequest) -> None:
        self._prs = list(prs)
        self.calls: list[tuple[str, str, str, str]] = []

    async def open_pr(self, head: str, base: str, title: str, body: str) -> PullRequest:
        self.calls.append((head, base, title, body))
        index = min(len(self.calls) - 1, len(self._prs) - 1)
        return self._prs[index]


def make_pr(number: int) -> PullRequest:
    return PullRequest(
        number=number,
        state="open",
        merged=False,
        head_ref=f"werft/run-{number}",
        head_sha="deadbeef",
        base_ref="unattended",
        mergeable=None,
        mergeable_state="unknown",
        html_url=f"https://github.test/o/r/pull/{number}",
    )


class SpyAlertSink:
    def __init__(self) -> None:
        self.review_waiting_calls: list[tuple[str, uuid.UUID, str]] = []
        self.run_parked_calls: list[tuple[str, uuid.UUID, str]] = []
        self.auth_failure_calls: list[str] = []
        self.project_flipped_calls: list[str] = []
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


class SpyQuota:
    def __init__(self) -> None:
        self.calls: list[tuple[object, uuid.UUID, int | None]] = []
        self.wake_calls: list[datetime | None] = []
        self.wake_at: datetime | None = None

    async def release(self, session, run, observed_seconds) -> None:
        self.calls.append((session, run.id, observed_seconds))

    async def next_wake_at(self, session, run, exhausted_until):
        self.wake_calls.append(exhausted_until)
        return self.wake_at or exhausted_until or (datetime.now(UTC) + timedelta(minutes=15))


# -- seeding -----------------------------------------------------------------


async def seed_project(
    session, *, lifecycle: str = "oracle_gated", unattended_branch: str = "unattended"
) -> Project:
    tag = uuid.uuid4().hex[:8]
    pid = (
        await session.execute(
            text(
                "INSERT INTO projects (slug, github_owner, github_repo, lifecycle, "
                "unattended_branch) VALUES (:slug, 'o', :repo, :lifecycle, :branch) "
                "RETURNING id"
            ),
            {
                "slug": f"p-{tag}",
                "repo": f"r-{tag}",
                "lifecycle": lifecycle,
                "branch": unattended_branch,
            },
        )
    ).scalar_one()
    await session.commit()
    return await session.get(Project, pid)


async def seed_backlog_item(
    session, project: Project, number: int, *, title: str = "an item"
) -> BacklogItem:
    bid = (
        await session.execute(
            text(
                "INSERT INTO backlog_items "
                "(project_id, github_issue_number, title, github_updated_at) "
                "VALUES (:p, :n, :t, now()) RETURNING id"
            ),
            {"p": project.id, "n": number, "t": title},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(BacklogItem, bid)


async def seed_running_run(
    session,
    project: Project,
    item: BacklogItem,
    *,
    attempt_count: int = 0,
    max_attempts: int = 3,
    provider: str = "claude",
) -> Run:
    rid = (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status, provider, "
                "attempt_count, max_attempts) "
                "VALUES (:p, :b, 'running', :prov, :ac, :ma) RETURNING id"
            ),
            {
                "p": project.id,
                "b": item.id,
                "prov": provider,
                "ac": attempt_count,
                "ma": max_attempts,
            },
        )
    ).scalar_one()
    await session.commit()
    return await session.get(Run, rid)


async def seed_open_attempt(
    session,
    run: Run,
    *,
    attempt_no: int = 1,
    started_seconds_ago: int = 5,
    provider: str = "claude",
) -> RunAttempt:
    aid = (
        await session.execute(
            text(
                "INSERT INTO run_attempts (run_id, attempt_no, provider, started_at) "
                "VALUES (:r, :n, :prov, now() - make_interval(secs => :offset)) "
                "RETURNING id"
            ),
            {"r": run.id, "n": attempt_no, "prov": provider, "offset": started_seconds_ago},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(RunAttempt, aid)


async def fresh_run(session, run_id) -> Run:
    return await session.get(Run, run_id, populate_existing=True)


async def fresh_attempt(session, attempt_id) -> RunAttempt:
    return await session.get(RunAttempt, attempt_id, populate_existing=True)


def success_classification() -> Classification:
    # doctrine #1: a clean-exit success carries no attempt outcome yet — the
    # oracle hasn't spoken, so nothing here is a verdict.
    return Classification(outcome=None, status=ResultStatus.SUCCESS, detail="ok")


# -- finalize_attempt: success + pushed --------------------------------------


async def test_success_pushed_oracle_gated_advances_to_awaiting_ci_pending_oracle(
    db_session,
) -> None:
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_running_run(db_session, project, item)
    attempt = await seed_open_attempt(db_session, run)
    ops = FakeRepoOps(make_pr(101))
    alerts = SpyAlertSink()
    quota = SpyQuota()

    await finalize_attempt(
        db_session,
        ops,
        run,
        project,
        classification=success_classification(),
        pushed=True,
        quota=quota,
        alerts=alerts,
    )

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "awaiting_ci"
    assert updated.pr_number == 101
    assert len(ops.calls) == 1

    updated_attempt = await fresh_attempt(db_session, attempt.id)
    assert updated_attempt.outcome is None  # pending oracle
    assert updated_attempt.ended_at is not None
    assert updated_attempt.duration_seconds is not None
    assert updated_attempt.duration_seconds >= 5

    assert alerts.review_waiting_calls == []  # oracle_gated never fires the bootstrap alert
    assert len(quota.calls) == 1


async def test_success_pushed_bootstrap_advances_to_awaiting_review_and_alerts_review_waiting(
    db_session,
) -> None:
    project = await seed_project(db_session, lifecycle="bootstrap")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_running_run(db_session, project, item)
    await seed_open_attempt(db_session, run)
    ops = FakeRepoOps(make_pr(202))
    alerts = SpyAlertSink()
    quota = SpyQuota()

    await finalize_attempt(
        db_session,
        ops,
        run,
        project,
        classification=success_classification(),
        pushed=True,
        quota=quota,
        alerts=alerts,
    )

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "awaiting_review"
    assert updated.pr_number == 202
    assert alerts.review_waiting_calls == [
        (project.slug, run.id, "https://github.test/o/r/pull/202")
    ]


# -- finalize_attempt: success without push ----------------------------------


async def test_success_without_push_records_agent_failure_outcome_and_requeues_with_backoff(
    db_session,
) -> None:
    """An agent that exits 0 but pushes nothing never reaches an oracle —
    doctrine #1's `outcome=NULL` ("pending oracle") encoding only makes sense
    for a run with a PR actually in flight for CI/review to judge. Leaving
    `outcome` NULL here would write a lying ledger row: a run that will
    never reach an oracle, forever encoded as "waiting on one". This must be
    recharacterized as a concrete `agent_failure` outcome and ride the same
    requeue/park ladder as any other genuine failure."""
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_running_run(db_session, project, item, attempt_count=0, max_attempts=3)
    attempt = await seed_open_attempt(db_session, run)
    alerts = SpyAlertSink()
    quota = SpyQuota()

    await finalize_attempt(
        db_session,
        FakeRepoOps(),
        run,
        project,
        classification=success_classification(),  # outcome=None, status=SUCCESS
        pushed=False,
        quota=quota,
        alerts=alerts,
    )

    updated_attempt = await fresh_attempt(db_session, attempt.id)
    assert updated_attempt.outcome == "agent_failure"  # not NULL: no lying "pending oracle" row
    assert updated_attempt.ended_at is not None

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "queued"  # budget left: requeues like any other genuine failure
    assert updated.attempt_count == 1
    assert updated.error_message == "success without push"
    assert alerts.run_parked_calls == []
    assert len(quota.calls) == 1


async def test_success_without_push_at_attempt_budget_parks_with_agent_failure_outcome(
    db_session,
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_running_run(db_session, project, item, attempt_count=2, max_attempts=3)
    attempt = await seed_open_attempt(db_session, run)
    alerts = SpyAlertSink()

    await finalize_attempt(
        db_session,
        FakeRepoOps(),
        run,
        project,
        classification=success_classification(),
        pushed=False,
        quota=SpyQuota(),
        alerts=alerts,
    )

    updated_attempt = await fresh_attempt(db_session, attempt.id)
    assert updated_attempt.outcome == "agent_failure"

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "parked"
    assert updated.attempt_count == 3
    assert updated.parked_reason == "agent_failure"
    assert alerts.run_parked_calls == [(project.slug, run.id, "agent_failure")]


# -- open_pr_and_wait: crash-window re-drive ---------------------------------


async def test_open_pr_and_wait_redriven_after_a_simulated_crash_advances_exactly_once(
    db_session,
) -> None:
    """Simulates a crash between `ops.open_pr` returning and the CAS
    committing: the caller doesn't know its own first call already
    succeeded, and calls `open_pr_and_wait` again with the same (now stale)
    view of the run it started with. The second `open_pr` lands on the real
    adopt-on-422 path and returns the *same* PR (this fake models that by
    replaying its one canned PR for every call past the first) — proving a
    genuine crash-and-retry converges on one PR number and one advance, with
    no exception.

    The "stale view" is deliberately a plain `SimpleNamespace`, not the
    `Run` instance `seed_running_run` returned: SQLAlchemy's ORM-enabled
    `UPDATE` auto-synchronizes already-loaded, identity-mapped objects
    matching its WHERE clause, so reusing that same `Run` object across both
    calls would silently pick up the first call's version bump — masking
    exactly the staleness this test exists to exercise.
    """
    project = await seed_project(db_session, lifecycle="oracle_gated")
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_running_run(db_session, project, item)
    stale_view = SimpleNamespace(
        id=run.id, version=run.version, backlog_item_id=run.backlog_item_id
    )
    ops = FakeRepoOps(make_pr(303))
    alerts = SpyAlertSink()

    await open_pr_and_wait(db_session, ops, stale_view, project, alerts=alerts)
    # re-drive with the same stale view (never refreshed after the first call)
    await open_pr_and_wait(db_session, ops, stale_view, project, alerts=alerts)

    assert len(ops.calls) == 2
    updated = await fresh_run(db_session, run.id)
    assert updated.status == "awaiting_ci"
    assert updated.pr_number == 303
    # `stale_view.version` (a bare namespace, never session-synchronized) is
    # the one reliable witness of the run's version before either call —
    # `run` itself would be silently mutated in place by `fresh_run`'s
    # `populate_existing` read, since `session.get` returns the same
    # identity-mapped object rather than a fresh copy.
    assert updated.version == stale_view.version + 1  # advanced exactly once, not twice


# -- finalize_attempt: running -> canceled race ------------------------------


async def test_finalize_attempt_loses_cas_to_an_out_of_band_cancel_and_returns_cleanly(
    db_session,
) -> None:
    """`running -> canceled` is itself a legal, routine edge (an operator can
    cancel a run while its container is still finishing) — it is not the
    only-genuinely-impossible case `finalize_attempt`'s `running -> failed`
    CAS loss must raise for. This seeds a running run with one open attempt,
    captures a stale view of the run's version, then transitions the *real*
    row to `canceled` out from under it (as a concurrent operator-cancel
    would) before calling `finalize_attempt` with the stale view. The
    `running -> failed` CAS must lose (stale version), but the attempt row
    is already closed and quota already released earlier in the same call —
    `finalize_attempt` must return cleanly, not raise and unwind that
    release, leaking the just-freed quota back out as unreserved headroom.
    """
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_running_run(db_session, project, item, attempt_count=0, max_attempts=3)
    attempt = await seed_open_attempt(db_session, run)
    # captured before the out-of-band transition below, same technique as
    # the open_pr_and_wait redrive test: a bare SimpleNamespace never
    # session-tracked, so it can't be silently synchronized to the new
    # version the way the identity-mapped `run` object would be.
    stale_view = SimpleNamespace(id=run.id, version=run.version)

    canceled = await transition_run(
        db_session, run_id=run.id, expected_version=run.version, new_status=RunStatus.CANCELED
    )
    assert canceled  # sanity: running -> canceled really is a legal edge
    await db_session.commit()

    classification = Classification(
        outcome=AttemptOutcome.AGENT_FAILURE, status=ResultStatus.FAILURE, detail="agent crashed"
    )
    quota = SpyQuota()
    alerts = SpyAlertSink()

    await finalize_attempt(
        db_session,
        FakeRepoOps(),
        stale_view,
        project,
        classification=classification,
        pushed=False,
        quota=quota,
        alerts=alerts,
    )  # must not raise

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "canceled"  # the race's outcome stands, untouched

    updated_attempt = await fresh_attempt(db_session, attempt.id)
    assert updated_attempt.ended_at is not None  # attempt row still closed
    assert updated_attempt.outcome == "agent_failure"

    assert len(quota.calls) == 1  # quota still released despite the lost CAS
    assert alerts.run_parked_calls == []
    assert alerts.auth_failure_calls == []


# -- advance_failed / finalize_attempt: quota_exhausted ----------------------


async def test_quota_exhausted_moves_to_blocked_quota_with_provider_reported_wake_time(
    db_session,
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_running_run(db_session, project, item, attempt_count=1, max_attempts=3)
    attempt = await seed_open_attempt(db_session, run)
    exhausted_until = datetime(2026, 8, 3, 20, 0, 0, tzinfo=UTC)
    classification = Classification(
        outcome=AttemptOutcome.QUOTA_EXHAUSTED,
        status=ResultStatus.QUOTA_EXHAUSTED,
        detail="provider window exhausted",
        exhausted_until=exhausted_until,
    )
    quota = SpyQuota()
    alerts = SpyAlertSink()

    await finalize_attempt(
        db_session,
        FakeRepoOps(),
        run,
        project,
        classification=classification,
        pushed=False,
        quota=quota,
        alerts=alerts,
    )

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "blocked_quota"
    assert updated.next_attempt_at == exhausted_until
    assert updated.attempt_count == 1  # unchanged: budget-exempt outcome
    assert len(quota.calls) == 1
    # SPEC §9 lists quota exhaustion among the six operator notifications;
    # before this it was the one sink method with no call site anywhere, so
    # a provider going dark until 20:00 was entirely silent.
    assert alerts.quota_exhausted_until_calls == [("claude", exhausted_until)]

    updated_attempt = await fresh_attempt(db_session, attempt.id)
    assert updated_attempt.outcome == "quota_exhausted"  # brief-mandated retry-ledger outcome


async def test_quota_exhausted_without_reported_until_falls_back_to_now_plus_15_minutes(
    db_session,
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_running_run(db_session, project, item)
    await seed_open_attempt(db_session, run)
    classification = Classification(
        outcome=AttemptOutcome.QUOTA_EXHAUSTED,
        status=ResultStatus.QUOTA_EXHAUSTED,
        detail="provider window exhausted",
        exhausted_until=None,
    )
    alerts = SpyAlertSink()

    before = datetime.now(UTC)
    await finalize_attempt(
        db_session,
        FakeRepoOps(),
        run,
        project,
        classification=classification,
        pushed=False,
        quota=SpyQuota(),
        alerts=alerts,
    )

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "blocked_quota"
    assert before + timedelta(minutes=14) < updated.next_attempt_at < before + timedelta(minutes=16)
    # No provider-reported window means nothing to tell an operator: the
    # 15 min guess is this system's own retry heuristic, not a fact about
    # the provider, and alerting it would be inventing information.
    assert alerts.quota_exhausted_until_calls == []


async def test_a_lost_cas_on_the_blocked_quota_edge_alerts_nobody(db_session) -> None:
    """The alert reports a transition; a transition that didn't happen must
    not be reported. Seeds a `failed` run, moves the real row out from under
    a stale view, and drives `advance_failed` with that stale view: the CAS
    loses, the (genuine, single-writer-violating) error propagates, and the
    sink stays untouched."""
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_running_run(db_session, project, item)
    await db_session.execute(
        text("UPDATE runs SET status = 'failed' WHERE id = :id"), {"id": run.id}
    )
    await db_session.commit()
    failed_run = await fresh_run(db_session, run.id)
    stale_view = SimpleNamespace(
        id=failed_run.id,
        version=failed_run.version,
        provider=failed_run.provider,
        attempt_count=failed_run.attempt_count,
        max_attempts=failed_run.max_attempts,
    )

    moved = await transition_run(
        db_session,
        run_id=run.id,
        expected_version=failed_run.version,
        new_status=RunStatus.QUEUED,
    )
    assert moved
    await db_session.commit()

    alerts = SpyAlertSink()
    with pytest.raises(RuntimeError):
        await advance_failed(
            db_session,
            stale_view,
            outcome=AttemptOutcome.QUOTA_EXHAUSTED,
            exhausted_until=datetime(2026, 8, 3, 20, 0, 0, tzinfo=UTC),
            quota=SpyQuota(),
            alerts=alerts,
        )

    assert alerts.quota_exhausted_until_calls == []


# -- advance_failed / finalize_attempt: genuine failures ---------------------


async def test_agent_failure_with_budget_left_requeues_with_exponential_backoff(db_session) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_running_run(db_session, project, item, attempt_count=0, max_attempts=3)
    attempt = await seed_open_attempt(db_session, run)
    classification = Classification(
        outcome=AttemptOutcome.AGENT_FAILURE, status=ResultStatus.FAILURE, detail="agent crashed"
    )
    alerts = SpyAlertSink()

    before = datetime.now(UTC)
    await finalize_attempt(
        db_session,
        FakeRepoOps(),
        run,
        project,
        classification=classification,
        pushed=False,
        quota=SpyQuota(),
        alerts=alerts,
    )

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "queued"
    assert updated.attempt_count == 1
    assert updated.error_message == "agent crashed"
    # backoff = min(2**1 * 30, 1800) = 60s
    assert before + timedelta(seconds=55) < updated.next_attempt_at < before + timedelta(seconds=65)
    assert alerts.run_parked_calls == []  # budget left: never parks, never alerts

    updated_attempt = await fresh_attempt(db_session, attempt.id)
    assert updated_attempt.outcome == "agent_failure"  # brief-mandated retry-ledger outcome


async def test_advance_failed_called_directly_moves_an_infra_failure_to_queued_with_backoff(
    db_session,
) -> None:
    """`advance_failed` is exercised through `finalize_attempt` everywhere
    else in this file; this test calls it directly on an already-`failed`
    row (the state `finalize_attempt`'s own CAS always lands it in first),
    proving decision 8's ladder is independently usable and covering
    `infra_failure` — a genuine-failure outcome no other test here uses."""
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_running_run(db_session, project, item, attempt_count=1, max_attempts=5)
    await db_session.execute(
        text("UPDATE runs SET status = 'failed' WHERE id = :id"), {"id": run.id}
    )
    await db_session.commit()
    failed_run = await fresh_run(db_session, run.id)
    classification = Classification(
        outcome=AttemptOutcome.INFRA_FAILURE, status=ResultStatus.FAILURE, detail="disk full"
    )

    await advance_failed(
        db_session,
        failed_run,
        outcome=classification.outcome,
        exhausted_until=classification.exhausted_until,
        quota=SpyQuota(),
        alerts=SpyAlertSink(),
    )

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "queued"
    assert updated.attempt_count == 2


async def test_infra_failure_at_budget_parks_with_the_infra_failure_reason_not_agent_failure(
    db_session,
) -> None:
    """`ParkedReason.INFRA_FAILURE` exists in the enum *and* in the DB's
    `parked_reason` CHECK constraint, so recording a disk-full or
    container-start failure as `agent_failure` was a plain miswiring, not
    the "no slot yet" limitation the docstring claimed. It is
    operator-facing: T6's review queue reads `parked_reason` directly, and
    "the agent failed three times" points a human at the prompt when the
    truth is that the infrastructure never let the agent run."""
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_running_run(db_session, project, item, attempt_count=2, max_attempts=3)
    await seed_open_attempt(db_session, run)
    classification = Classification(
        outcome=AttemptOutcome.INFRA_FAILURE, status=ResultStatus.FAILURE, detail="disk full"
    )
    alerts = SpyAlertSink()

    await finalize_attempt(
        db_session,
        FakeRepoOps(),
        run,
        project,
        classification=classification,
        pushed=False,
        quota=SpyQuota(),
        alerts=alerts,
    )

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "parked"
    assert updated.parked_reason == "infra_failure"
    assert alerts.run_parked_calls == [(project.slug, run.id, "infra_failure")]


async def test_agent_failure_at_attempt_budget_parks_and_alerts_run_parked(db_session) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_running_run(db_session, project, item, attempt_count=2, max_attempts=3)
    await seed_open_attempt(db_session, run)
    classification = Classification(
        outcome=AttemptOutcome.AGENT_FAILURE,
        status=ResultStatus.FAILURE,
        detail="agent crashed again",
    )
    alerts = SpyAlertSink()

    await finalize_attempt(
        db_session,
        FakeRepoOps(),
        run,
        project,
        classification=classification,
        pushed=False,
        quota=SpyQuota(),
        alerts=alerts,
    )

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "parked"
    assert updated.attempt_count == 3
    assert updated.parked_reason == "agent_failure"
    assert alerts.run_parked_calls == [(project.slug, run.id, "agent_failure")]


async def test_auth_failure_with_budget_left_requeues_and_fires_auth_failure_alert(
    db_session,
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_running_run(
        db_session, project, item, attempt_count=0, max_attempts=3, provider="claude"
    )
    await seed_open_attempt(db_session, run)
    classification = Classification(
        outcome=AttemptOutcome.AUTH_FAILURE,
        status=ResultStatus.AUTH_FAILURE,
        detail="bad credentials",
    )
    alerts = SpyAlertSink()

    await finalize_attempt(
        db_session,
        FakeRepoOps(),
        run,
        project,
        classification=classification,
        pushed=False,
        quota=SpyQuota(),
        alerts=alerts,
    )

    updated = await fresh_run(db_session, run.id)
    # decision 8 (thin): auth_failure has no dedicated edge — it rides the
    # same queued/parked ladder as every other genuine failure.
    assert updated.status == "queued"
    assert alerts.auth_failure_calls == ["claude"]
    assert alerts.run_parked_calls == []  # budget left: never parks


async def test_auth_failure_at_budget_parks_with_generic_agent_failure_reason_not_auth_failure(
    db_session,
) -> None:
    """Provider-specific parked reasons are post-milestone fallthrough
    (decision 8): the DB's parked_reason CHECK constraint has no
    'auth_failure' slot, so a budget-spent auth_failure still parks with the
    generic 'agent_failure' reason, same as any other exhausted genuine
    failure — only the alert distinguishes it."""
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_running_run(db_session, project, item, attempt_count=2, max_attempts=3)
    await seed_open_attempt(db_session, run)
    classification = Classification(
        outcome=AttemptOutcome.AUTH_FAILURE,
        status=ResultStatus.AUTH_FAILURE,
        detail="bad credentials",
    )
    alerts = SpyAlertSink()

    await finalize_attempt(
        db_session,
        FakeRepoOps(),
        run,
        project,
        classification=classification,
        pushed=False,
        quota=SpyQuota(),
        alerts=alerts,
    )

    updated = await fresh_run(db_session, run.id)
    assert updated.status == "parked"
    assert updated.parked_reason == "agent_failure"
    assert alerts.auth_failure_calls == ["claude"]
    assert alerts.run_parked_calls == [(project.slug, run.id, "agent_failure")]


# -- quota.release: same-transaction guarantee -------------------------------


async def test_quota_release_called_exactly_once_in_the_same_session_before_any_commit(
    db_session,
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_running_run(db_session, project, item)
    await seed_open_attempt(db_session, run)
    classification = Classification(
        outcome=AttemptOutcome.AGENT_FAILURE, status=ResultStatus.FAILURE, detail="boom"
    )
    quota = SpyQuota()

    # deliberately never call db_session.commit() before this assertion —
    # release() must already have been invoked, on this exact session, as
    # part of the still-open transaction the caller will commit afterward.
    await finalize_attempt(
        db_session,
        FakeRepoOps(),
        run,
        project,
        classification=classification,
        pushed=False,
        quota=quota,
        alerts=SpyAlertSink(),
    )

    assert len(quota.calls) == 1
    seen_session, seen_run_id, observed_seconds = quota.calls[0]
    assert seen_session is db_session
    assert seen_run_id == run.id
    assert observed_seconds is not None
