"""The spine: reference tables, projects, backlog, runs, events, attempts, quota, artifacts.

Hand-written — the authoritative DDL for SPEC §3 (state machine + triggers),
§7 (quota tables), §8 (artifact metadata). Seed rows for run_statuses and
run_status_transitions are frozen literals; the contract test in
tests/integration/test_db_contract.py asserts identity with werft.domain.

Revision ID: 0001
Revises:
Create Date: 2026-08-01

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (status, is_terminal) — SPEC §3.2: terminal is merged/canceled ONLY.
RUN_STATUSES = [
    ("queued", False),
    ("claimed", False),
    ("running", False),
    ("awaiting_ci", False),
    ("awaiting_review", False),
    ("merging", False),
    ("blocked_quota", False),
    ("failed", False),
    ("parked", False),
    ("merged", True),
    ("canceled", True),
]

# The 34 legal transitions — SPEC §3.2 verbatim (25 named + 9 x "-> canceled").
TRANSITIONS = [
    ("queued", "claimed"),
    ("queued", "blocked_quota"),
    ("queued", "parked"),
    ("queued", "canceled"),
    ("claimed", "queued"),
    ("claimed", "running"),
    ("claimed", "failed"),
    ("claimed", "canceled"),
    ("running", "awaiting_ci"),
    ("running", "awaiting_review"),
    ("running", "failed"),
    ("running", "canceled"),
    ("awaiting_ci", "queued"),
    ("awaiting_ci", "merging"),
    ("awaiting_ci", "failed"),
    ("awaiting_ci", "parked"),
    ("awaiting_ci", "canceled"),
    ("awaiting_review", "merging"),
    ("awaiting_review", "parked"),
    ("awaiting_review", "failed"),
    ("awaiting_review", "canceled"),
    ("merging", "merged"),
    ("merging", "awaiting_ci"),
    ("merging", "parked"),
    ("merging", "failed"),
    ("merging", "canceled"),
    ("blocked_quota", "queued"),
    ("blocked_quota", "canceled"),
    ("failed", "queued"),
    ("failed", "blocked_quota"),
    ("failed", "parked"),
    ("failed", "canceled"),
    ("parked", "queued"),
    ("parked", "canceled"),
]


def upgrade() -> None:
    # --- 1. Reference tables + seeds -------------------------------------
    op.execute(
        """
        CREATE TABLE providers (
            code TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('subscription', 'local'))
        )
        """
    )
    op.execute("INSERT INTO providers (code, kind) VALUES ('claude', 'subscription')")

    op.execute("CREATE TABLE run_statuses (status TEXT PRIMARY KEY, is_terminal BOOLEAN NOT NULL)")
    statuses = ", ".join(f"('{s}', {t})" for s, t in RUN_STATUSES)
    op.execute(f"INSERT INTO run_statuses (status, is_terminal) VALUES {statuses}")

    op.execute(
        """
        CREATE TABLE run_status_transitions (
            from_status TEXT NOT NULL REFERENCES run_statuses (status),
            to_status   TEXT NOT NULL REFERENCES run_statuses (status),
            PRIMARY KEY (from_status, to_status)
        )
        """
    )
    pairs = ", ".join(f"('{f}', '{t}')" for f, t in TRANSITIONS)
    op.execute(f"INSERT INTO run_status_transitions (from_status, to_status) VALUES {pairs}")

    # --- 2. projects (SPEC §3.1, §6.2) ------------------------------------
    op.execute(
        """
        CREATE TABLE projects (
            id                 UUID PRIMARY KEY DEFAULT uuidv7(),
            slug               TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]+$'),
            github_owner       TEXT NOT NULL,
            github_repo        TEXT NOT NULL,
            main_branch        TEXT NOT NULL DEFAULT 'main',
            unattended_branch  TEXT NOT NULL DEFAULT 'unattended',
            lifecycle          TEXT NOT NULL DEFAULT 'bootstrap'
                               CHECK (lifecycle IN ('bootstrap', 'oracle_gated')),
            merge_mode         TEXT NOT NULL DEFAULT 'strict_serialized'
                               CHECK (merge_mode = 'strict_serialized'),
            is_paused          BOOLEAN NOT NULL DEFAULT false,
            ci_timeout_seconds INT NOT NULL DEFAULT 21600,
            onboarded_at       TIMESTAMPTZ,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (github_owner, github_repo)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE project_events (
            id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            project_id UUID NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            payload    JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_project_events_project ON project_events (project_id, id)")

    # --- 3. backlog_items (doctrine #5; SPEC §3.3.6, §6.2) -----------------
    op.execute(
        """
        CREATE TABLE backlog_items (
            id                  UUID PRIMARY KEY DEFAULT uuidv7(),
            project_id          UUID NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
            github_issue_number INT NOT NULL,
            title               TEXT NOT NULL,
            body                TEXT NOT NULL DEFAULT '',
            labels              TEXT[] NOT NULL DEFAULT '{}',
            is_eligible         BOOLEAN NOT NULL DEFAULT true,
            github_updated_at   TIMESTAMPTZ NOT NULL,
            synced_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (project_id, github_issue_number)
        )
        """
    )

    # --- 4. runs (SPEC §3.2/§3.3) ------------------------------------------
    op.execute(
        """
        CREATE TABLE runs (
            id                  UUID PRIMARY KEY DEFAULT uuidv7(),
            project_id          UUID NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
            backlog_item_id     UUID NOT NULL REFERENCES backlog_items (id),
            status              TEXT NOT NULL DEFAULT 'queued' REFERENCES run_statuses (status),
            version             INT NOT NULL DEFAULT 0,
            priority            SMALLINT NOT NULL DEFAULT 100,
            provider            TEXT REFERENCES providers (code),
            attempt_count       SMALLINT NOT NULL DEFAULT 0,
            max_attempts        SMALLINT NOT NULL DEFAULT 3,
            next_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            lease_expires_at    TIMESTAMPTZ,
            last_heartbeat_at   TIMESTAMPTZ,
            hard_deadline_at    TIMESTAMPTZ,
            branch_name         TEXT,
            base_sha            TEXT,
            container_id        TEXT,
            runner_image_digest TEXT,
            exit_code           INT,
            pr_number           INT,
            merge_commit_sha    TEXT,
            files_changed       INT,
            lines_added         INT,
            lines_deleted       INT,
            parked_reason       TEXT CHECK (parked_reason IN
                                ('ci_red', 'merge_conflict', 'merge_blocked', 'ci_timeout',
                                 'agent_failure', 'infra_failure', 'permanent_error',
                                 'deadline', 'review_rejected')),
            result              JSONB,
            error_message       TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_runs_claimable ON runs (priority DESC, created_at) WHERE status = 'queued'"
    )
    op.execute(
        "CREATE INDEX ix_runs_lease_reaper ON runs (lease_expires_at)"
        " WHERE status IN ('claimed', 'running')"
    )
    op.execute(
        "CREATE INDEX ix_runs_deadline ON runs (hard_deadline_at)"
        " WHERE status IN ('claimed', 'running')"
    )
    op.execute(
        "CREATE INDEX ix_runs_ci_wait ON runs (updated_at)"
        " WHERE status IN ('awaiting_ci', 'merging')"
    )
    op.execute("CREATE INDEX ix_runs_project_status ON runs (project_id, status)")
    op.execute(
        "CREATE UNIQUE INDEX ux_runs_one_active_per_item ON runs (backlog_item_id)"
        " WHERE status NOT IN ('merged', 'canceled')"
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_runs_pr ON runs (project_id, pr_number) WHERE pr_number IS NOT NULL"
    )

    # --- 5. run_events -------------------------------------------------------
    op.execute(
        """
        CREATE TABLE run_events (
            id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            run_id     UUID NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            payload    JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_run_events_run ON run_events (run_id, id)")
    op.execute("CREATE INDEX ix_run_events_ts ON run_events (created_at)")

    # --- 6. run_attempts (typed outcomes from day one; SPEC §3.2) -----------
    op.execute(
        """
        CREATE TABLE run_attempts (
            id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            run_id           UUID NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
            attempt_no       SMALLINT NOT NULL,
            provider         TEXT NOT NULL REFERENCES providers (code),
            behavior         TEXT NOT NULL DEFAULT 'retry' CHECK (behavior = 'retry'),
            outcome          TEXT CHECK (outcome IN
                             ('ci_green', 'ci_red', 'agent_failure', 'infra_failure',
                              'quota_exhausted', 'auth_failure', 'policy_block',
                              'timeout', 'canceled')),
            duration_seconds INT,
            started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            ended_at         TIMESTAMPTZ,
            UNIQUE (run_id, attempt_no)
        )
        """
    )

    # --- 7. quota (SPEC §7 minimal: one dimension, one knob) -----------------
    op.execute(
        """
        CREATE TABLE provider_accounts (
            id                               UUID PRIMARY KEY DEFAULT uuidv7(),
            provider                         TEXT NOT NULL REFERENCES providers (code),
            label                            TEXT NOT NULL DEFAULT 'primary',
            rolling_window_hours             INT NOT NULL DEFAULT 5,
            ceiling_seconds                  INT NOT NULL,
            window_cap_runs                  INT,
            provider_window_capacity_seconds INT,
            exhausted_until                  TIMESTAMPTZ,
            exhausted_source                 TEXT,
            last_reading_utilization         NUMERIC(5, 2),
            last_reading_source              TEXT,
            last_reading_at                  TIMESTAMPTZ,
            is_active                        BOOLEAN NOT NULL DEFAULT true,
            UNIQUE (provider, label)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE quota_ledger (
            id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            provider_account_id  UUID NOT NULL REFERENCES provider_accounts (id),
            run_id               UUID NOT NULL REFERENCES runs (id),
            attempt_no           SMALLINT NOT NULL,
            model                TEXT,
            reserved_wallclock_s INT NOT NULL,
            actual_wallclock_s   INT,
            consumed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (run_id, attempt_no)
        )
        """
    )

    # --- 8. artifacts (SPEC §8: bytes on disk, metadata in DB) ---------------
    op.execute(
        """
        CREATE TABLE artifacts (
            id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            run_id       UUID NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
            path         TEXT NOT NULL,
            bytes        BIGINT NOT NULL,
            collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            content_hash TEXT,
            event_ref    BIGINT REFERENCES run_events (id),
            UNIQUE (run_id, path)
        )
        """
    )

    # --- 9. Triggers (SPEC §3.2 enforcement; lineage A§4.4) ------------------
    op.execute(
        """
        CREATE FUNCTION runs_enforce_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status IS DISTINCT FROM OLD.status THEN
                IF NOT EXISTS (SELECT 1 FROM run_status_transitions t
                               WHERE t.from_status = OLD.status
                                 AND t.to_status = NEW.status) THEN
                    RAISE EXCEPTION 'illegal run status transition: % -> %',
                        OLD.status, NEW.status;
                END IF;
                INSERT INTO run_events (run_id, event_type, payload)
                VALUES (NEW.id, 'status_changed',
                        jsonb_build_object('from', OLD.status, 'to', NEW.status,
                                           'version', NEW.version));
                PERFORM pg_notify('werft_events',
                                  json_build_object('t', 'run', 'id', NEW.id)::text);
            END IF;
            NEW.updated_at := now();
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_runs_transition BEFORE UPDATE ON runs
            FOR EACH ROW EXECUTE FUNCTION runs_enforce_transition()
        """
    )

    op.execute(
        """
        CREATE FUNCTION runs_emit_created() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO run_events (run_id, event_type, payload)
            VALUES (NEW.id, 'created', jsonb_build_object('status', NEW.status));
            PERFORM pg_notify('werft_events',
                              json_build_object('t', 'run', 'id', NEW.id)::text);
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_runs_created AFTER INSERT ON runs
            FOR EACH ROW EXECUTE FUNCTION runs_emit_created()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_runs_created ON runs")
    op.execute("DROP FUNCTION runs_emit_created()")
    op.execute("DROP TRIGGER trg_runs_transition ON runs")
    op.execute("DROP FUNCTION runs_enforce_transition()")
    for table in (
        "artifacts",
        "quota_ledger",
        "provider_accounts",
        "run_attempts",
        "run_events",
        "runs",
        "backlog_items",
        "project_events",
        "projects",
        "run_status_transitions",
        "run_statuses",
        "providers",
    ):
        op.execute(f"DROP TABLE {table}")
