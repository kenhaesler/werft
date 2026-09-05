"""Pydantic response models for `/api/v1` (SPEC §9 operator surface: runs
list, run detail, quota status, artifact listing).

The field set and names below are the pinned dashboard contract — the T5
dashboard is already built against this exact shape (`dashboard/src/lib/
types.ts`), so a renamed or dropped field here breaks it silently on the
wire, not at import time.
"""

from datetime import datetime
from typing import Any, Literal
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


class ActivityOperation(BaseModel):
    kind: str
    key: str


class ActivityWorker(BaseModel):
    state: Literal["idle", "running", "waiting", "error"]
    current_operation: ActivityOperation | None
    last_started_at: datetime | None
    last_completed_at: datetime | None
    last_error_at: datetime | None
    waiting_until: datetime | None


class RecentOperation(BaseModel):
    worker: str
    kind: str
    key: str
    outcome: Literal["succeeded", "failed"]
    started_at: datetime
    completed_at: datetime
    duration_ms: int


class ManagerActivityOut(BaseModel):
    available: bool
    unavailable_reason: str | None
    started_at: datetime | None
    workers: dict[str, ActivityWorker]
    recent_operations: list[RecentOperation]
    live_driver_run_ids: list[UUID]


class ActivityEvent(BaseModel):
    id: int
    run_id: UUID
    project_slug: str
    issue_number: int
    issue_title: str
    run_status: str
    event_type: str
    phase: str | None
    from_status: str | None
    to_status: str | None
    created_at: datetime


class ActivityRun(BaseModel):
    run_id: UUID
    project_slug: str
    issue_number: int
    issue_title: str
    status: str
    parked_reason: str | None
    provider: str | None
    container_id: str | None
    attempt_started_at: datetime | None
    last_heartbeat_at: datetime | None
    lease_expires_at: datetime | None
    hard_deadline_at: datetime | None
    next_attempt_at: datetime
    updated_at: datetime


class ActivityResponse(BaseModel):
    generated_at: datetime
    manager: ManagerActivityOut
    status_counts: dict[str, int]
    recent_events: list[ActivityEvent]
    active_runs_total: int
    active_runs_limit: int
    active_runs: list[ActivityRun]


class OnboardRequest(BaseModel):
    """`POST /api/v1/projects/onboard`'s body (SPEC §6.3) — the closed set
    of fields `onboard_project` needs; main/unattended branch names stay at
    their defaults ("main"/"unattended"), not exposed here."""

    slug: str
    owner: str
    repo: str


class FlipRequest(BaseModel):
    """`POST /api/v1/projects/{id}/flip`'s body (SPEC §3.1) — the manual
    repair flip, either direction."""

    to: Literal["oracle_gated", "bootstrap"]


class ProjectOut(BaseModel):
    """The project shape both `onboard` (201) and `flip` (200) return —
    `owner`/`repo` mirror `OnboardRequest`'s own field names rather than the
    `github_owner`/`github_repo` column names underneath."""

    id: UUID
    slug: str
    owner: str
    repo: str
    lifecycle: str
    onboarded_at: datetime | None
    created_at: datetime
