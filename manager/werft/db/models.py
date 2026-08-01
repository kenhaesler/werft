"""Query-side mirror of the schema.

DDL truth lives in werft/db/migrations (hand-written; SPEC §3). These models
mirror columns for the app's queries — no column exists here that the
migration does not create, and CHECK constraints are enforced by the DDL
and the domain enums, not repeated here.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Provider(Base):
    __tablename__ = "providers"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)


class RunStatusRow(Base):
    __tablename__ = "run_statuses"

    status: Mapped[str] = mapped_column(Text, primary_key=True)
    is_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False)


class RunStatusTransition(Base):
    __tablename__ = "run_status_transitions"

    from_status: Mapped[str] = mapped_column(
        Text, ForeignKey("run_statuses.status"), primary_key=True
    )
    to_status: Mapped[str] = mapped_column(
        Text, ForeignKey("run_statuses.status"), primary_key=True
    )


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("github_owner", "github_repo"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    github_owner: Mapped[str] = mapped_column(Text, nullable=False)
    github_repo: Mapped[str] = mapped_column(Text, nullable=False)
    main_branch: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'main'"))
    unattended_branch: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unattended'")
    )
    lifecycle: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'bootstrap'"))
    merge_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'strict_serialized'")
    )
    is_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    ci_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("21600")
    )
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ProjectEvent(Base):
    __tablename__ = "project_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class BacklogItem(Base):
    __tablename__ = "backlog_items"
    __table_args__ = (UniqueConstraint("project_id", "github_issue_number"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    github_issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    labels: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    is_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    github_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    backlog_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backlog_items.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Text, ForeignKey("run_statuses.status"), nullable=False, server_default=text("'queued'")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("100"))
    provider: Mapped[str | None] = mapped_column(Text, ForeignKey("providers.code"))
    attempt_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("3")
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hard_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    branch_name: Mapped[str | None] = mapped_column(Text)
    base_sha: Mapped[str | None] = mapped_column(Text)
    container_id: Mapped[str | None] = mapped_column(Text)
    runner_image_digest: Mapped[str | None] = mapped_column(Text)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    merge_commit_sha: Mapped[str | None] = mapped_column(Text)
    files_changed: Mapped[int | None] = mapped_column(Integer)
    lines_added: Mapped[int | None] = mapped_column(Integer)
    lines_deleted: Mapped[int | None] = mapped_column(Integer)
    parked_reason: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class RunEvent(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class RunAttempt(Base):
    __tablename__ = "run_attempts"
    __table_args__ = (UniqueConstraint("run_id", "attempt_no"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    provider: Mapped[str] = mapped_column(Text, ForeignKey("providers.code"), nullable=False)
    behavior: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'retry'"))
    outcome: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderAccount(Base):
    __tablename__ = "provider_accounts"
    __table_args__ = (UniqueConstraint("provider", "label"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    provider: Mapped[str] = mapped_column(Text, ForeignKey("providers.code"), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'primary'"))
    rolling_window_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("5")
    )
    ceiling_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    window_cap_runs: Mapped[int | None] = mapped_column(Integer)
    provider_window_capacity_seconds: Mapped[int | None] = mapped_column(Integer)
    exhausted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exhausted_source: Mapped[str | None] = mapped_column(Text)
    last_reading_utilization: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    last_reading_source: Mapped[str | None] = mapped_column(Text)
    last_reading_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class QuotaLedgerEntry(Base):
    __tablename__ = "quota_ledger"
    __table_args__ = (UniqueConstraint("run_id", "attempt_no"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    provider_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_accounts.id"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    reserved_wallclock_s: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_wallclock_s: Mapped[int | None] = mapped_column(Integer)
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("run_id", "path"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    content_hash: Mapped[str | None] = mapped_column(Text)
    event_ref: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("run_events.id"))
