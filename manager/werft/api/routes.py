"""HTTP routes (SPEC §9 operator surface): runs list, run detail, quota
status, artifact listing, and the six-endpoint mutation set — review
accept/reject, run cancel/requeue, project onboard, and the manual
lifecycle flip (Task 12/B3's CLOSED write set; see `_run_summary_query`'s
callers below for the exact list — artifact *serving* is the one surface
still outstanding).

Two routers, wired separately by the composition root in `app.py`:
`healthz_router` carries no auth dependency (the watchdog must always be
able to reach it, token configured or not); `api_router` is mounted under
`/api/v1` with the bearer-auth dependency attached at `include_router`
time, once `app.py` has read the token file.
"""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from werft.api.schemas import (
    ArtifactOut,
    ArtifactsResponse,
    ArtifactSummary,
    FlipRequest,
    OnboardRequest,
    ProjectOut,
    QuotaAccount,
    QuotaResponse,
    RunAttemptOut,
    RunDetail,
    RunEventOut,
    RunsListResponse,
    RunSummary,
)
from werft.db.models import (
    Artifact,
    BacklogItem,
    Project,
    ProviderAccount,
    QuotaLedgerEntry,
    Run,
    RunAttempt,
    RunEvent,
)
from werft.db.transitions import transition_run
from werft.domain.errors import PermanentError
from werft.domain.projects import ProjectLifecycle
from werft.domain.runs import TERMINAL_STATUSES, ParkedReason, RunStatus
from werft.orchestrator.ci_watch import flip_project
from werft.orchestrator.merge_flow import advance_merging
from werft.orchestrator.onboard import onboard_project

logger = structlog.get_logger(__name__)

healthz_router = APIRouter()


@healthz_router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """DB session dependency: opens a session from the `async_sessionmaker`
    the composition root built once, in `app.py`'s lifespan, and stashed on
    `app.state` (SPEC §1 one-engine guarantee — this never builds its own
    engine). Integration tests bypass it entirely via
    `app.dependency_overrides[get_session]`, injecting the `db_session`
    fixture's session directly."""
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


api_router = APIRouter()


def _pr_url(owner: str, repo: str, pr_number: int | None) -> str | None:
    if pr_number is None:
        return None
    return f"https://github.com/{owner}/{repo}/pull/{pr_number}"


def _latest_outcome_subquery():
    """The `run_attempts.outcome` of the row with the highest `attempt_no`
    for a correlated `Run` (a scalar subquery, not a join — a run can have
    several attempts and both `list_runs` and `get_run` need exactly one
    row per run). Null when the run has no attempts yet, or when its latest
    attempt hasn't reported an outcome."""
    return (
        select(RunAttempt.outcome)
        .where(RunAttempt.run_id == Run.id)
        .order_by(RunAttempt.attempt_no.desc())
        .limit(1)
        .correlate(Run)
        .scalar_subquery()
    )


def _run_summary_query():
    """The `RunSummary` column set plus its two joins, unfiltered — shared
    by `list_runs` (paged, optionally filtered) and `_get_run_summary`
    below (one row, by id) so both ever define the shape exactly once."""
    latest_outcome = _latest_outcome_subquery()
    return (
        select(
            Run.id,
            Project.slug.label("project_slug"),
            Run.status,
            BacklogItem.github_issue_number.label("issue_number"),
            BacklogItem.title.label("issue_title"),
            Run.attempt_count,
            Run.max_attempts,
            latest_outcome.label("latest_outcome"),
            Run.parked_reason,
            Run.pr_number,
            Project.github_owner,
            Project.github_repo,
            Run.created_at,
            Run.updated_at,
        )
        .join(Project, Run.project_id == Project.id)
        .join(BacklogItem, Run.backlog_item_id == BacklogItem.id)
    )


def _row_to_run_summary(row) -> RunSummary:
    return RunSummary(
        id=row.id,
        project_slug=row.project_slug,
        status=row.status,
        issue_number=row.issue_number,
        issue_title=row.issue_title,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        latest_outcome=row.latest_outcome,
        parked_reason=row.parked_reason,
        pr_number=row.pr_number,
        pr_url=_pr_url(row.github_owner, row.github_repo, row.pr_number),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@api_router.get("/runs", response_model=RunsListResponse)
async def list_runs(
    status: str | None = None,
    project: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> RunsListResponse:
    """`GET /api/v1/runs` — `RunSummary` rows ordered `created_at DESC, id
    DESC` (SPEC §9; the `id` tiebreak keeps pagination stable across rows
    that share a `created_at`). `pr_url` is derived from the project's
    `github_owner`/`github_repo` plus `pr_number`, never stored.
    """
    base = _run_summary_query()
    if status is not None:
        base = base.where(Run.status == status)
    if project is not None:
        base = base.where(Project.slug == project)

    count_result = await session.execute(select(func.count()).select_from(base.subquery()))
    total = count_result.scalar_one()

    # `created_at` alone is not a stable sort key: Postgres's `now()` (the
    # column default) is transaction-start time, so two runs inserted in
    # separate statements within the same transaction — or two concurrent
    # transactions — can land on the exact same timestamp. `Run.id` is a
    # uuidv7 (time-ordered), so it breaks ties deterministically without
    # reordering rows that already differ by `created_at`.
    page = base.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit).offset(offset)
    rows = (await session.execute(page)).all()

    runs = [_row_to_run_summary(row) for row in rows]
    return RunsListResponse(runs=runs, total=total)


async def _get_run_or_404(session: AsyncSession, run_id: UUID) -> None:
    """Raises 404 when `run_id` has no `runs` row. Shared by the two
    `/runs/{id}/...` sub-resource routes so a nonexistent run's artifacts
    404 rather than silently answering with an empty list — a request for
    a subresource of nothing is itself an unknown resource, not an empty
    one."""
    exists = (await session.execute(select(Run.id).where(Run.id == run_id))).first()
    if exists is None:
        raise HTTPException(status_code=404, detail="run not found")


@api_router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> RunDetail:
    """`GET /api/v1/runs/{id}` — `RunSummary`'s fields plus the detail-only
    columns and the three related-row collections (SPEC §9). Returned
    directly, not wrapped in an envelope. A syntactically invalid UUID
    never reaches this function: FastAPI/pydantic validate the `run_id:
    UUID` path param first and answer 422 on its own.
    """
    latest_outcome = _latest_outcome_subquery()
    stmt = (
        select(
            Run.id,
            Project.slug.label("project_slug"),
            Run.status,
            BacklogItem.github_issue_number.label("issue_number"),
            BacklogItem.title.label("issue_title"),
            Run.attempt_count,
            Run.max_attempts,
            latest_outcome.label("latest_outcome"),
            Run.parked_reason,
            Run.pr_number,
            Project.github_owner,
            Project.github_repo,
            Run.created_at,
            Run.updated_at,
            Run.branch_name,
            Run.base_sha,
            Run.merge_commit_sha,
            Run.error_message,
            Run.result,
        )
        .join(Project, Run.project_id == Project.id)
        .join(BacklogItem, Run.backlog_item_id == BacklogItem.id)
        .where(Run.id == run_id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")

    # Chronological order: the `id` identity column is a strictly
    # increasing bigint, so it doubles as an insertion-order tiebreak
    # without a second sort key — the same reasoning `list_runs` applies to
    # `Run.id` for `created_at` ties.
    event_rows = (
        await session.execute(
            select(RunEvent.id, RunEvent.event_type, RunEvent.payload, RunEvent.created_at)
            .where(RunEvent.run_id == run_id)
            .order_by(RunEvent.id.asc())
        )
    ).all()
    attempt_rows = (
        await session.execute(
            select(
                RunAttempt.attempt_no,
                RunAttempt.provider,
                RunAttempt.outcome,
                RunAttempt.duration_seconds,
                RunAttempt.started_at,
                RunAttempt.ended_at,
            )
            .where(RunAttempt.run_id == run_id)
            .order_by(RunAttempt.attempt_no.asc())
        )
    ).all()
    artifact_rows = (
        await session.execute(
            select(Artifact.path, Artifact.bytes, Artifact.collected_at)
            .where(Artifact.run_id == run_id)
            .order_by(Artifact.path.asc())
        )
    ).all()

    return RunDetail(
        id=row.id,
        project_slug=row.project_slug,
        status=row.status,
        issue_number=row.issue_number,
        issue_title=row.issue_title,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        latest_outcome=row.latest_outcome,
        parked_reason=row.parked_reason,
        pr_number=row.pr_number,
        pr_url=_pr_url(row.github_owner, row.github_repo, row.pr_number),
        created_at=row.created_at,
        updated_at=row.updated_at,
        branch_name=row.branch_name,
        base_sha=row.base_sha,
        merge_commit_sha=row.merge_commit_sha,
        error_message=row.error_message,
        result=row.result,
        events=[
            RunEventOut(
                id=e.id, event_type=e.event_type, payload=e.payload, created_at=e.created_at
            )
            for e in event_rows
        ],
        attempts=[
            RunAttemptOut(
                attempt_no=a.attempt_no,
                provider=a.provider,
                outcome=a.outcome,
                duration_seconds=a.duration_seconds,
                started_at=a.started_at,
                ended_at=a.ended_at,
            )
            for a in attempt_rows
        ],
        artifacts=[
            ArtifactSummary(path=art.path, bytes=art.bytes, collected_at=art.collected_at)
            for art in artifact_rows
        ],
    )


@api_router.get("/runs/{run_id}/artifacts", response_model=ArtifactsResponse)
async def list_artifacts(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> ArtifactsResponse:
    """`GET /api/v1/runs/{id}/artifacts` — the full artifact rows (SPEC §8:
    metadata in DB, bytes on disk; this endpoint serves the metadata only —
    file serving is a later task). 404 when the run itself doesn't exist;
    an empty list is a valid answer for a real run with no collected
    artifacts yet.
    """
    await _get_run_or_404(session, run_id)

    rows = (
        await session.execute(
            select(Artifact.path, Artifact.bytes, Artifact.collected_at, Artifact.content_hash)
            .where(Artifact.run_id == run_id)
            .order_by(Artifact.path.asc())
        )
    ).all()
    artifacts = [
        ArtifactOut(
            path=row.path,
            bytes=row.bytes,
            collected_at=row.collected_at,
            content_hash=row.content_hash,
        )
        for row in rows
    ]
    return ArtifactsResponse(artifacts=artifacts)


@api_router.get("/quota", response_model=QuotaResponse)
async def get_quota(
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> QuotaResponse:
    """`GET /api/v1/quota` — one row per `provider_accounts` row (SPEC §7:
    consumed/reserved/ceiling/headroom/exhaustion/last-reading, each its own
    field — the operator is never asked to do arithmetic).

    `consumed_seconds` and `reserved_seconds` are disjoint buckets over the
    *same* `quota_ledger` rows, partitioned on `actual_wallclock_s IS
    [NOT] NULL` — every ledger row is counted in exactly one of the two, so
    `headroom_seconds = ceiling - consumed - reserved` never double-counts
    a row. (An earlier version had `consumed_seconds` fall back to
    `reserved_wallclock_s` via `COALESCE` for still-open rows — that made
    an in-window, unresolved reservation land in *both* sums and subtract
    twice from headroom; review caught it, this is the fix.)

    `consumed_seconds` sums `actual_wallclock_s` for ledger entries that
    have reported one, within the account's own `rolling_window_hours`.
    `reserved_seconds` is a *live-commitment* figure, not a windowed one:
    it sums `reserved_wallclock_s` for every entry that has no
    `actual_wallclock_s` yet, regardless of age — an attempt that started
    outside the rolling window and never resolved is still an outstanding
    claim on capacity. `headroom_seconds` is `ceiling - consumed - reserved`,
    floored at zero rather than surfaced negative. Whether an in-window,
    *still-open* reservation should also nudge `consumed_seconds` (a
    "how much of my window am I about to spend" reading) is a possible
    refinement left to T7, which owns the reservation lifecycle end to
    end; this endpoint only reads what's already in the ledger.
    """
    # `make_interval`'s positional signature is (years, months, weeks, days,
    # hours, mins, secs); zeros fill the leading params so the fifth
    # position lands on `rolling_window_hours`, matching
    # `test_api_runs.py`'s own `make_interval(secs => :offset)` seeding
    # idiom one column over.
    window_floor = func.now() - func.make_interval(0, 0, 0, 0, ProviderAccount.rolling_window_hours)

    consumed_subq = (
        select(func.coalesce(func.sum(QuotaLedgerEntry.actual_wallclock_s), 0))
        .where(QuotaLedgerEntry.provider_account_id == ProviderAccount.id)
        .where(QuotaLedgerEntry.actual_wallclock_s.is_not(None))
        .where(QuotaLedgerEntry.consumed_at > window_floor)
        .correlate(ProviderAccount)
        .scalar_subquery()
    )
    reserved_subq = (
        select(func.coalesce(func.sum(QuotaLedgerEntry.reserved_wallclock_s), 0))
        .where(QuotaLedgerEntry.provider_account_id == ProviderAccount.id)
        .where(QuotaLedgerEntry.actual_wallclock_s.is_(None))
        .correlate(ProviderAccount)
        .scalar_subquery()
    )

    stmt = select(
        ProviderAccount.provider,
        ProviderAccount.label,
        ProviderAccount.ceiling_seconds,
        consumed_subq.label("consumed_seconds"),
        reserved_subq.label("reserved_seconds"),
        ProviderAccount.exhausted_until,
        ProviderAccount.exhausted_source,
        ProviderAccount.last_reading_utilization,
        ProviderAccount.last_reading_source,
        ProviderAccount.last_reading_at,
    ).order_by(ProviderAccount.provider, ProviderAccount.label)

    rows = (await session.execute(stmt)).all()

    accounts = [
        QuotaAccount(
            provider=row.provider,
            label=row.label,
            ceiling_seconds=row.ceiling_seconds,
            consumed_seconds=row.consumed_seconds,
            reserved_seconds=row.reserved_seconds,
            headroom_seconds=max(
                row.ceiling_seconds - row.consumed_seconds - row.reserved_seconds, 0
            ),
            exhausted_until=row.exhausted_until,
            exhausted_source=row.exhausted_source,
            last_reading_utilization=row.last_reading_utilization,
            last_reading_source=row.last_reading_source,
            last_reading_at=row.last_reading_at,
        )
        for row in rows
    ]
    return QuotaResponse(accounts=accounts)


# -- mutations (SPEC §9, the closed write set — Task 12/B3) ------------------
#
# Every run mutation below follows the same shape: fetch-or-404, check the
# precondition *in Python* before ever attempting the CAS, then
# `transition_run`. Checking the precondition first means the only way the
# CAS itself can still lose is a genuine concurrent race (another request
# moved the row between the read and the `UPDATE`) — the DB trigger's own
# "illegal run status transition" exception is never reached either way,
# because a version-matched-but-status-wrong row can't occur (this code path
# is the only writer of `runs.status`, and status/version always move
# together), and a lost race means zero rows match the `WHERE ... AND
# version = ...`, so the trigger never fires at all. That is what keeps a
# wrong-state or raced request a 409, never an unhandled 500 from a raised
# DB exception.


async def _get_run_summary(session: AsyncSession, run_id: UUID) -> RunSummary:
    """Re-reads one run in the exact `RunSummary` shape `list_runs` returns
    (controller ruling: every run mutation answers with the refreshed run,
    unwrapped, same shape as the list) — a plain column `select`, not
    `session.get`, so it always reflects what was just committed rather than
    a possibly-stale identity-mapped `Run` instance."""
    row = (await session.execute(_run_summary_query().where(Run.id == run_id))).one()
    return _row_to_run_summary(row)


def _project_out(project: Project) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        slug=project.slug,
        owner=project.github_owner,
        repo=project.github_repo,
        lifecycle=project.lifecycle,
        onboarded_at=project.onboarded_at,
        created_at=project.created_at,
    )


async def _get_run_for_mutation(session: AsyncSession, run_id: UUID) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@api_router.post("/runs/{run_id}/review/accept", response_model=RunSummary)
async def accept_review(
    run_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> RunSummary:
    """`POST /api/v1/runs/{id}/review/accept` — CAS `awaiting_review ->
    merging` (SPEC §9), 409 on the wrong current state or a lost CAS race.

    On a won CAS, drives one best-effort inline `advance_merging` tick
    (controller ruling: run mutations are GitHub-less-safe) — skipped
    entirely when `app.state.ops_for` is `None` (no GitHub creds
    configured), and every error out of it is swallowed (logged, never
    raised): the 30 s poller tick is the actual guarantee this accept only
    tries to shortcut, so a failure here must never turn an accepted review
    into a 500.
    """
    run = await _get_run_for_mutation(session, run_id)
    if run.status != RunStatus.AWAITING_REVIEW.value:
        raise HTTPException(status_code=409, detail=f"run is {run.status}, not awaiting_review")

    ok = await transition_run(
        session, run_id=run_id, expected_version=run.version, new_status=RunStatus.MERGING
    )
    if not ok:
        raise HTTPException(status_code=409, detail="run state changed concurrently")
    await session.commit()

    ops_for = request.app.state.ops_for
    if ops_for is not None:
        run = await session.get(Run, run_id, populate_existing=True)
        project = await session.get(Project, run.project_id)
        try:
            await advance_merging(
                session, ops_for(project), run, project, alerts=request.app.state.alerts
            )
            await session.commit()
        except Exception:
            await session.rollback()
            logger.warning("api.accept_advance_merging_failed", run_id=str(run_id), exc_info=True)

    return await _get_run_summary(session, run_id)


@api_router.post("/runs/{run_id}/review/reject", response_model=RunSummary)
async def reject_review(
    run_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> RunSummary:
    """`POST /api/v1/runs/{id}/review/reject` — CAS `awaiting_review ->
    parked` with `parked_reason='review_rejected'` (SPEC §9), then fires
    `alerts.run_parked` — only after the CAS has won, the same "alert after
    the write, never before" discipline `merge_flow._park` and
    `ci_watch.advance_awaiting_ci` use.
    """
    run = await _get_run_for_mutation(session, run_id)
    if run.status != RunStatus.AWAITING_REVIEW.value:
        raise HTTPException(status_code=409, detail=f"run is {run.status}, not awaiting_review")

    ok = await transition_run(
        session,
        run_id=run_id,
        expected_version=run.version,
        new_status=RunStatus.PARKED,
        extra={"parked_reason": ParkedReason.REVIEW_REJECTED.value},
    )
    if not ok:
        raise HTTPException(status_code=409, detail="run state changed concurrently")
    await session.commit()

    project = await session.get(Project, run.project_id)
    await request.app.state.alerts.run_parked(
        project.slug, run_id, ParkedReason.REVIEW_REJECTED.value
    )

    return await _get_run_summary(session, run_id)


@api_router.post("/runs/{run_id}/cancel", response_model=RunSummary)
async def cancel_run(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> RunSummary:
    """`POST /api/v1/runs/{id}/cancel` — CAS `<non-terminal> -> canceled`
    (SPEC §9; SPEC §3.2's canceled edges are every non-terminal status), 409
    if the run is already terminal. Cleanup (closing any open PR, deleting
    the run branch) happens out-of-band via the tick sweep's
    `cleanup_terminal` — this endpoint only flips the status.
    """
    run = await _get_run_for_mutation(session, run_id)
    if run.status in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"run is already {run.status}")

    ok = await transition_run(
        session, run_id=run_id, expected_version=run.version, new_status=RunStatus.CANCELED
    )
    if not ok:
        raise HTTPException(status_code=409, detail="run state changed concurrently")
    await session.commit()

    return await _get_run_summary(session, run_id)


@api_router.post("/runs/{run_id}/requeue", response_model=RunSummary)
async def requeue_run(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> RunSummary:
    """`POST /api/v1/runs/{id}/requeue` — CAS `parked -> queued`, resetting
    `attempt_count` to 0 and `next_attempt_at` to now (plan Behavioral
    decision 7: a human explicitly granting a fresh retry budget — not a
    resume of the one that was already spent). 409 from any state but
    `parked`.
    """
    run = await _get_run_for_mutation(session, run_id)
    if run.status != RunStatus.PARKED.value:
        raise HTTPException(status_code=409, detail=f"run is {run.status}, not parked")

    ok = await transition_run(
        session,
        run_id=run_id,
        expected_version=run.version,
        new_status=RunStatus.QUEUED,
        extra={"attempt_count": 0, "next_attempt_at": func.now()},
    )
    if not ok:
        raise HTTPException(status_code=409, detail="run state changed concurrently")
    await session.commit()

    return await _get_run_summary(session, run_id)


@api_router.post("/projects/onboard", response_model=ProjectOut, status_code=201)
async def onboard(
    body: OnboardRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> ProjectOut:
    """`POST /api/v1/projects/onboard` — SPEC §6.3's one-time bootstrap
    setup, driven through `orchestrator/onboard.py`'s `onboard_project`.
    Requires GitHub creds (controller ruling: unlike the run mutations,
    onboard cannot proceed without them) — 503 when `app.state.ops_for`/
    `admin_ops_for` are unset. `onboard_project`'s `PermanentError` maps to
    409 for a duplicate slug/repo, 422 for an unreachable installation
    (main branch/repo not found).

    No `Project` row exists yet for `app.state.ops_for`'s per-project cache
    key to key off of — a throwaway `SimpleNamespace` carrying just the
    owner/repo the operator supplied plus a fresh id stands in; the
    factories only ever read those three attributes off whatever they're
    given (see `app.py::_ops_factory`/`_admin_ops_factory`).
    """
    ops_for = request.app.state.ops_for
    admin_ops_for = request.app.state.admin_ops_for
    if ops_for is None or admin_ops_for is None:
        raise HTTPException(status_code=503, detail="GitHub App not configured")

    target = SimpleNamespace(id=uuid4(), github_owner=body.owner, github_repo=body.repo)
    try:
        project = await onboard_project(
            session,
            ops_for(target),
            admin_ops_for(target),
            slug=body.slug,
            owner=body.owner,
            repo=body.repo,
        )
    except PermanentError as exc:
        await session.rollback()
        message = str(exc)
        status_code = 409 if "already onboarded" in message else 422
        raise HTTPException(status_code=status_code, detail=message) from exc

    await session.commit()
    return _project_out(project)


@api_router.post("/projects/{project_id}/flip", response_model=ProjectOut)
async def flip(
    project_id: UUID,
    body: FlipRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> ProjectOut:
    """`POST /api/v1/projects/{id}/flip` — the manual repair flip (SPEC
    §3.1), either direction, delegating to `ci_watch.flip_project` (the same
    idempotent guard the automatic doctrine-#1 flip uses). Requires GitHub
    creds (503 when unconfigured, same controller ruling as onboard); a
    no-op guard miss (project already at the requested `to` lifecycle) is
    409, never a silent 200.
    """
    admin_ops_for = request.app.state.admin_ops_for
    if admin_ops_for is None:
        raise HTTPException(status_code=503, detail="GitHub App not configured")

    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    to = ProjectLifecycle(body.to)
    flipped = await flip_project(
        session, admin_ops_for(project), project, to=to, alerts=request.app.state.alerts
    )
    if not flipped:
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"project already {to.value}")
    await session.commit()

    project = await session.get(Project, project_id, populate_existing=True)
    return _project_out(project)
