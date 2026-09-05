"""Read-only capability and project audit endpoints.

The response deliberately reports configuration separately from verification.
It never serializes settings paths or credential material.
"""

import asyncio
from datetime import datetime
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from werft.api.routes import get_session
from werft.config.dispatch import DispatchConfig, load_dispatch_config
from werft.config.settings import Settings
from werft.db.models import Project, ProjectEvent

capabilities_router = APIRouter()


class DispatchCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str
    configured: bool
    schema_validated: bool
    provider: str
    model: str | None = None
    image_digest: str | None = None
    timeout_seconds: int | None = None
    memory_bytes: int | None = None
    nano_cpus: int | None = None
    registries: list[str] = Field(default_factory=list)
    extra_hosts: list[str] = Field(default_factory=list)
    egress_hosts: list[str] = Field(default_factory=list)
    mode: str | None = None


class CapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: dict[str, bool]
    readiness: dict[str, str]
    dispatch: list[DispatchCapability]


class ProjectEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    payload: dict
    created_at: datetime


class ProjectEventsResponse(BaseModel):
    total: int
    events: list[ProjectEventOut]


def _settings(request: Request) -> Settings:
    """Read the single settings object resolved by the composition root."""
    return request.app.state.settings


async def _safe_dispatch(settings: Settings) -> tuple[DispatchConfig, str]:
    if not settings.dispatch_config_file:
        return DispatchConfig(), "unconfigured"
    if not Path(settings.dispatch_config_file).is_file():
        return DispatchConfig(), "missing"
    try:
        config = await asyncio.to_thread(load_dispatch_config, settings.dispatch_config_file)
    except Exception:
        return DispatchConfig(), "invalid"
    return config, "validated"


@capabilities_router.get("/capabilities", response_model=CapabilitiesResponse)
async def capabilities(
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> CapabilitiesResponse:
    settings = _settings(request)
    dispatch, dispatch_status = await _safe_dispatch(settings)
    projects = (await session.execute(select(Project))).scalars().all()
    modes = {project.slug: project.lifecycle for project in projects}
    entries = [
        DispatchCapability(
            project=slug,
            configured=slug in dispatch.projects,
            schema_validated=dispatch_status == "validated" and slug in dispatch.projects,
            provider=settings.quota_provider,
            model=value.model if value else None,
            image_digest=value.image_digest if value else None,
            timeout_seconds=value.timeout_seconds if value else None,
            memory_bytes=value.memory_bytes if value else None,
            nano_cpus=value.nano_cpus if value else None,
            registries=value.registries if value else [],
            extra_hosts=value.extra_hosts if value else [],
            egress_hosts=value.egress_hosts() if value else [],
            mode=modes.get(slug),
        )
        for slug in sorted(set(dispatch.projects) | set(modes))
        for value in [dispatch.projects.get(slug)]
    ]
    configured = {
        "github": bool(settings.github_app_client_id and settings.github_app_private_key_file),
        "dispatch": bool(settings.dispatch_config_file),
        "provider_credential": bool(settings.claude_credential_file),
        "quota": settings.quota_ceiling_seconds > 0,
        "docker": bool(settings.docker_url),
        "egress": settings.egress_slot_count > 0,
    }
    readiness = {
        name: ("configured" if value else "unconfigured") for name, value in configured.items()
    }
    readiness["dispatch"] = dispatch_status
    return CapabilitiesResponse(capabilities=configured, readiness=readiness, dispatch=entries)


@capabilities_router.get("/projects/{project_id}/events", response_model=ProjectEventsResponse)
async def project_events(
    project_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> ProjectEventsResponse:
    project_count = await session.scalar(
        select(func.count()).select_from(Project).where(Project.id == project_id)
    )
    if not project_count:
        raise HTTPException(status_code=404, detail="project not found")
    total = await session.scalar(
        select(func.count()).select_from(ProjectEvent).where(ProjectEvent.project_id == project_id)
    )
    rows = (
        (
            await session.execute(
                select(ProjectEvent)
                .where(ProjectEvent.project_id == project_id)
                .order_by(ProjectEvent.created_at.desc(), ProjectEvent.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return ProjectEventsResponse(total=total or 0, events=rows)
