"""HTTP routes (SPEC §9 operator surface, thin-loop minimum: this task
ships only the runs list — artifact listing/serving, review accept/reject,
run cancel/requeue, project onboard/flip, and quota status land in later
tasks).

Two routers, wired separately by the composition root in `app.py`:
`healthz_router` carries no auth dependency (the watchdog must always be
able to reach it, token configured or not); `api_router` is mounted under
`/api/v1` with the bearer-auth dependency attached at `include_router`
time, once `app.py` has read the token file.
"""

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from werft.api.schemas import RunsListResponse, RunSummary
from werft.db.models import BacklogItem, Project, Run, RunAttempt

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


@api_router.get("/runs", response_model=RunsListResponse)
async def list_runs(
    status: str | None = None,
    project: str | None = None,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> RunsListResponse:
    """`GET /api/v1/runs` — `RunSummary` rows ordered `created_at DESC`
    (SPEC §9). `latest_outcome` is the `run_attempts.outcome` of the row
    with the highest `attempt_no` for that run (a correlated scalar
    subquery, not a join — a run can have several attempts and this must
    stay one row per run); null when the run has no attempts yet, or when
    its latest attempt hasn't reported an outcome. `pr_url` is derived from
    the project's `github_owner`/`github_repo` plus `pr_number`, never
    stored.
    """
    latest_outcome = (
        select(RunAttempt.outcome)
        .where(RunAttempt.run_id == Run.id)
        .order_by(RunAttempt.attempt_no.desc())
        .limit(1)
        .correlate(Run)
        .scalar_subquery()
    )

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

    page = base.order_by(Run.created_at.desc()).limit(limit).offset(offset)
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
