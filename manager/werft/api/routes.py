"""HTTP routes (SPEC §9 operator surface, thin-loop minimum: this task
ships the runs list, run detail, quota status, and artifact listing —
artifact *serving*, review accept/reject, run cancel/requeue, and project
onboard/flip land in later tasks).

Two routers, wired separately by the composition root in `app.py`:
`healthz_router` carries no auth dependency (the watchdog must always be
able to reach it, token configured or not); `api_router` is mounted under
`/api/v1` with the bearer-auth dependency attached at `include_router`
time, once `app.py` has read the token file.
"""

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from werft.api.schemas import (
    ArtifactOut,
    ArtifactsResponse,
    ArtifactSummary,
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
    latest_outcome = _latest_outcome_subquery()

    base = (
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

    runs = [
        RunSummary(
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
        for row in rows
    ]
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

    `consumed_seconds` sums `COALESCE(actual_wallclock_s, reserved_wallclock_s)`
    for ledger entries within the account's own `rolling_window_hours` —
    the provisional reservation stands in for the actual until the attempt
    reports one, so an in-flight attempt still counts toward utilization.
    `reserved_seconds` is a *live-commitment* figure, not a windowed one:
    it sums `reserved_wallclock_s` for every entry that has no
    `actual_wallclock_s` yet, regardless of age — an attempt that started
    outside the rolling window and never resolved is still an outstanding
    claim on capacity. `headroom_seconds` is `ceiling - consumed - reserved`,
    floored at zero rather than surfaced negative.
    """
    # `make_interval`'s positional signature is (years, months, weeks, days,
    # hours, mins, secs); zeros fill the leading params so the fifth
    # position lands on `rolling_window_hours`, matching
    # `test_api_runs.py`'s own `make_interval(secs => :offset)` seeding
    # idiom one column over.
    window_floor = func.now() - func.make_interval(0, 0, 0, 0, ProviderAccount.rolling_window_hours)

    consumed_subq = (
        select(
            func.coalesce(
                func.sum(
                    func.coalesce(
                        QuotaLedgerEntry.actual_wallclock_s, QuotaLedgerEntry.reserved_wallclock_s
                    )
                ),
                0,
            )
        )
        .where(QuotaLedgerEntry.provider_account_id == ProviderAccount.id)
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
