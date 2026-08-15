"""Pydantic response models for `/api/v1` (SPEC §9 operator surface: runs
list, run detail, quota status, artifact listing).

The field set and names below are the pinned dashboard contract — the T5
dashboard is already built against this exact shape (`dashboard/src/lib/
types.ts`), so a renamed or dropped field here breaks it silently on the
wire, not at import time.
"""

from datetime import datetime
from typing import Any
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


class RunEventOut(BaseModel):
    id: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class RunAttemptOut(BaseModel):
    attempt_no: int
    provider: str
    outcome: str | None
    duration_seconds: int | None
    started_at: datetime
    ended_at: datetime | None


class ArtifactSummary(BaseModel):
    """The artifact shape embedded in `RunDetail` — deliberately narrower
    than `ArtifactOut` (no `content_hash`); the standalone artifact-listing
    endpoint carries the full row."""

    path: str
    bytes: int
    collected_at: datetime


class RunDetail(BaseModel):
    """`RunSummary`'s fields plus the detail-only columns and the three
    related-row collections (SPEC §9). Unlike `RunsListResponse`, this is
    returned directly — not wrapped in an envelope object."""

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
    branch_name: str | None
    base_sha: str | None
    merge_commit_sha: str | None
    error_message: str | None
    result: dict[str, Any] | None
    events: list[RunEventOut]
    attempts: list[RunAttemptOut]
    artifacts: list[ArtifactSummary]


class QuotaAccount(BaseModel):
    provider: str
    label: str
    ceiling_seconds: int
    consumed_seconds: int
    reserved_seconds: int
    headroom_seconds: int
    exhausted_until: datetime | None
    exhausted_source: str | None
    last_reading_utilization: float | None
    last_reading_source: str | None
    last_reading_at: datetime | None


class QuotaResponse(BaseModel):
    accounts: list[QuotaAccount]


class ArtifactOut(BaseModel):
    path: str
    bytes: int
    collected_at: datetime
    content_hash: str | None


class ArtifactsResponse(BaseModel):
    artifacts: list[ArtifactOut]
