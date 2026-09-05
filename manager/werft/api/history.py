"""Paginated durable task event history for the operator workspace."""

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from werft.api.routes import get_session
from werft.api.schemas import ActivityEvent
from werft.db.models import BacklogItem, Project, Run, RunEvent

history_router = APIRouter()


def _literal_like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@history_router.get("/events")
async def event_history(
    project: str | None = Query(default=None),
    q: str = Query(default="", max_length=200),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> dict[str, Any]:
    if not isinstance(project, str):
        project = None
    if not isinstance(q, str):
        q = ""
    if not isinstance(limit, int):
        limit = 20
    if not isinstance(offset, int):
        offset = 0
    filters = []
    if project:
        filters.append(Project.slug == project)
    if q:
        pattern = _literal_like(q)
        filters.append(
            or_(
                Project.slug.ilike(pattern, escape="\\"),
                BacklogItem.title.ilike(pattern, escape="\\"),
                RunEvent.event_type.ilike(pattern, escape="\\"),
                cast(RunEvent.payload, String).ilike(pattern, escape="\\"),
            )
        )
    base = (
        select(RunEvent.id)
        .join(Run, Run.id == RunEvent.run_id)
        .join(Project, Project.id == Run.project_id)
        .join(BacklogItem, BacklogItem.id == Run.backlog_item_id)
        .where(*filters)
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = (
        await session.execute(
            select(
                RunEvent.id,
                RunEvent.run_id,
                Project.slug.label("project_slug"),
                BacklogItem.github_issue_number.label("issue_number"),
                BacklogItem.title.label("issue_title"),
                Run.status.label("run_status"),
                RunEvent.event_type,
                RunEvent.payload,
                RunEvent.created_at,
            )
            .join(Run, Run.id == RunEvent.run_id)
            .join(Project, Project.id == Run.project_id)
            .join(BacklogItem, BacklogItem.id == Run.backlog_item_id)
            .where(*filters)
            .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    events = [
        ActivityEvent(
            id=row.id,
            run_id=row.run_id,
            project_slug=row.project_slug,
            issue_number=row.issue_number,
            issue_title=row.issue_title,
            run_status=row.run_status,
            event_type=row.event_type,
            phase=row.payload.get("phase") if isinstance(row.payload, dict) else None,
            from_status=row.payload.get("from") if isinstance(row.payload, dict) else None,
            to_status=row.payload.get("to") if isinstance(row.payload, dict) else None,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return {
        "total": total,
        "events": [
            event.model_dump() | {"payload": row.payload}
            for event, row in zip(events, rows, strict=True)
        ],
    }
