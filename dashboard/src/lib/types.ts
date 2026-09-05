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
