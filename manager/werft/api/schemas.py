"""Pydantic response models for `/api/v1` (SPEC §9 operator surface, first
vertical slice: the runs list).

The field set and names below are the pinned dashboard contract — the T5
dashboard is already built against this exact shape, so a renamed or
dropped field here breaks it silently on the wire, not at import time.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class RunSummary(BaseModel):
    id: UUID
    project_slug: str
    status: str
    issue_number: int
    issue_title: str
    attempt_count: int
    max_attempts: int
    latest_outcome: str | None
    parked_reason: str | None
    pr_number: int | None
    pr_url: str | None
    created_at: datetime
    updated_at: datetime


class RunsListResponse(BaseModel):
    runs: list[RunSummary]
    total: int
