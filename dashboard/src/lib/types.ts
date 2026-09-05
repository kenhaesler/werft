export type RunStatus =
  | 'queued'
  | 'claimed'
  | 'running'
  | 'awaiting_ci'
  | 'awaiting_review'
  | 'merging'
  | 'blocked_quota'
  | 'failed'
  | 'parked'
  | 'merged'
  | 'canceled';

export interface RunSummary {
  id: string;
  project_slug: string;
  status: RunStatus;
  issue_number: number;
  issue_title: string;
  attempt_count: number;
  max_attempts: number;
  latest_outcome: string | null;
  parked_reason: string | null;
  pr_number: number | null;
  pr_url: string | null;
  created_at: string;
  updated_at: string;
}

export interface RunsResponse {
  runs: RunSummary[];
  total: number;
}

export interface QuotaAccount {
  provider: string;
  label: string;
  ceiling_seconds: number;
  consumed_seconds: number;
  reserved_seconds: number;
  headroom_seconds: number;
  exhausted_until: string | null;
  exhausted_source: string | null;
  last_reading_utilization: number | null;
  last_reading_source: string | null;
  last_reading_at: string | null;
}

export interface QuotaResponse {
  accounts: QuotaAccount[];
}

export interface Artifact {
  path: string;
  bytes: number;
  collected_at: string;
  content_hash: string | null;
}

export interface ArtifactsResponse {
  artifacts: Artifact[];
}

export interface RunDetail extends RunSummary {
  branch_name: string | null;
  base_sha: string | null;
  merge_commit_sha: string | null;
  error_message: string | null;
  result: Record<string, unknown> | null;
  events: {
    id: number;
    event_type: string;
    payload: Record<string, unknown>;
    created_at: string;
  }[];
  attempts: {
    attempt_no: number;
    provider: string;
    outcome: string | null;
    duration_seconds: number | null;
    started_at: string;
    ended_at: string | null;
  }[];
  artifacts: Omit<Artifact, 'content_hash'>[];
}

export interface Project {
  id: string;
  slug: string;
  owner: string;
  repo: string;
  lifecycle: string;
  onboarded_at: string | null;
  created_at: string;
}

export interface Machine {
  name: string;
  os: string;
  architecture: string;
  engine_version: string;
  cpus: number;
  memory_bytes: number;
  max_concurrent_runs: number;
  containers: {
    id: string;
    run_id: string;
    name: string;
    image: string;
    state: string;
    status: string;
  }[];
}

export interface ActivityWorker {
  state: 'idle' | 'running' | 'waiting' | 'error';
  current_operation: { kind: string; key: string } | null;
  last_started_at: string | null;
  last_completed_at: string | null;
  last_error_at: string | null;
  waiting_until: string | null;
}

export interface ActivityRun {
  run_id: string;
  project_slug: string;
  issue_number: number;
  issue_title: string;
  status: RunStatus;
  provider: string | null;
  container_id: string | null;
  attempt_started_at: string | null;
  last_heartbeat_at: string | null;
  lease_expires_at: string | null;
  hard_deadline_at: string | null;
  next_attempt_at: string;
  parked_reason: string | null;
  updated_at: string;
}

export interface ActivitySnapshot {
  generated_at: string;
  manager: {
    available: boolean;
    unavailable_reason?: string | null;
    started_at: string | null;
    workers: Record<string, ActivityWorker>;
    recent_operations: {
      worker: string;
      kind: string;
      key: string;
      outcome: 'succeeded' | 'failed';
      started_at: string;
      completed_at: string;
      duration_ms: number;
    }[];
    live_driver_run_ids: string[];
  };
  status_counts: Partial<Record<RunStatus, number>>;
  recent_events: {
    id: number;
    run_id: string;
    project_slug: string;
    issue_number: number;
    issue_title: string;
    run_status: RunStatus;
    event_type: string;
    phase: string | null;
    from_status: string | null;
    to_status: string | null;
    created_at: string;
  }[];
  active_runs: ActivityRun[];
  active_runs_total: number;
}
