import type { Machine, Project, QuotaResponse, RunDetail, RunSummary } from './types';

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();
export const demoProjects: Project[] = [
  {
    id: 'project-1',
    slug: 'atlas-web',
    owner: 'demo',
    repo: 'atlas-web',
    lifecycle: 'oracle_gated',
    onboarded_at: ago(4000),
    created_at: ago(4000),
  },
  {
    id: 'project-2',
    slug: 'data-pipeline',
    owner: 'demo',
    repo: 'data-pipeline',
    lifecycle: 'oracle_gated',
    onboarded_at: ago(5000),
    created_at: ago(5000),
  },
  {
    id: 'project-3',
    slug: 'design-system',
    owner: 'demo',
    repo: 'design-system',
    lifecycle: 'bootstrap',
    onboarded_at: ago(3000),
    created_at: ago(3000),
  },
];
export const demoRuns: RunSummary[] = [
  {
    id: 'demo-01',
    project_slug: 'atlas-web',
    status: 'running',
    issue_number: 128,
    issue_title: 'Build the new analytics workspace',
    attempt_count: 1,
    max_attempts: 3,
    latest_outcome: null,
    parked_reason: null,
    pr_number: null,
    pr_url: null,
    created_at: ago(12),
    updated_at: ago(1),
  },
  {
    id: 'demo-02',
    project_slug: 'data-pipeline',
    status: 'awaiting_ci',
    issue_number: 64,
    issue_title: 'Add incremental sync to the ingestion service',
    attempt_count: 1,
    max_attempts: 3,
    latest_outcome: 'success',
    parked_reason: null,
    pr_number: 72,
    pr_url: null,
    created_at: ago(24),
    updated_at: ago(3),
  },
  {
    id: 'demo-03',
    project_slug: 'design-system',
    status: 'awaiting_review',
    issue_number: 38,
    issue_title: 'Create accessible command menu primitives',
    attempt_count: 1,
    max_attempts: 3,
    latest_outcome: 'success',
    parked_reason: null,
    pr_number: 41,
    pr_url: null,
    created_at: ago(42),
    updated_at: ago(8),
  },
  {
    id: 'demo-04',
    project_slug: 'atlas-web',
    status: 'merged',
    issue_number: 124,
    issue_title: 'Improve authentication session handling',
    attempt_count: 2,
    max_attempts: 3,
    latest_outcome: 'success',
    parked_reason: null,
    pr_number: 132,
    pr_url: null,
    created_at: ago(83),
    updated_at: ago(36),
  },
  {
    id: 'demo-05',
    project_slug: 'data-pipeline',
    status: 'queued',
    issue_number: 67,
    issue_title: 'Add retry policies for upstream timeouts',
    attempt_count: 0,
    max_attempts: 3,
    latest_outcome: null,
    parked_reason: null,
    pr_number: null,
    pr_url: null,
    created_at: ago(95),
    updated_at: ago(44),
  },
  {
    id: 'demo-06',
    project_slug: 'atlas-web',
    status: 'merged',
    issue_number: 122,
    issue_title: 'Reduce bundle size on the settings route',
    attempt_count: 1,
    max_attempts: 3,
    latest_outcome: 'success',
    parked_reason: null,
    pr_number: 130,
    pr_url: null,
    created_at: ago(155),
    updated_at: ago(67),
  },
  {
    id: 'demo-07',
    project_slug: 'design-system',
    status: 'parked',
    issue_number: 35,
    issue_title: 'Document the new motion guidelines',
    attempt_count: 3,
    max_attempts: 3,
    latest_outcome: 'max_turns',
    parked_reason: 'attempts_exhausted',
    pr_number: null,
    pr_url: null,
    created_at: ago(200),
    updated_at: ago(110),
  },
];
export const demoQuota: QuotaResponse = {
  accounts: [
    {
      provider: 'claude',
      label: 'Primary account',
      ceiling_seconds: 18000,
      consumed_seconds: 6840,
      reserved_seconds: 2700,
      headroom_seconds: 8460,
      exhausted_until: null,
      exhausted_source: null,
      last_reading_utilization: 38,
      last_reading_source: 'provider',
      last_reading_at: ago(2),
    },
  ],
};
export const demoMachine: Machine = {
  name: 'werft-production',
  os: 'Ubuntu 24.04 LTS',
  architecture: 'x86_64',
  engine_version: '29.6.2',
  cpus: 8,
  memory_bytes: 16 * 1024 ** 3,
  max_concurrent_runs: 4,
  containers: [
    {
      id: 'a8f093bd24c1',
      run_id: 'demo-01',
      name: 'runner-atlas-128',
      image: 'werft/runner:latest',
      state: 'running',
      status: 'Up 12 minutes',
    },
    {
      id: 'b37ea124ac01',
      run_id: 'demo-02',
      name: 'runner-pipeline-64',
      image: 'werft/runner:latest',
      state: 'running',
      status: 'Up 24 minutes',
    },
  ],
};
export function demoDetail(run: RunSummary): RunDetail {
  const result: RunDetail = {
    ...run,
    branch_name: `werft/${run.project_slug}-${run.issue_number}`,
    base_sha: '0f4a8c27',
    merge_commit_sha: run.status === 'merged' ? 'c843ead1' : null,
    error_message: null,
    result:
      run.latest_outcome === 'success'
        ? {
            summary:
              'Implementation complete. Changes and verification evidence are ready for inspection.',
          }
        : null,
    attempts: [
      {
        attempt_no: run.attempt_count,
        provider: 'claude',
        outcome: run.latest_outcome,
        duration_seconds: 684,
        started_at: run.created_at,
        ended_at: run.latest_outcome ? run.updated_at : null,
      },
    ],
    events: [
      {
        id: 1,
        event_type: 'run.claimed',
        payload: { message: 'Approved issue claimed. Provider quota reserved.' },
        created_at: run.created_at,
      },
      {
        id: 2,
        event_type: 'runner.started',
        payload: { message: 'Isolated workspace prepared. Agent runtime started.' },
        created_at: new Date(
          (Date.parse(run.created_at) + Date.parse(run.updated_at)) / 2,
        ).toISOString(),
      },
      {
        id: 3,
        event_type: 'status_changed',
        payload: { from: 'claimed', to: run.status },
        created_at: run.updated_at,
      },
    ],
    artifacts: [
      { path: 'transcript.jsonl', bytes: 128400, collected_at: ago(1) },
      { path: 'changes.diff', bytes: 16400, collected_at: ago(1) },
    ],
  };
  if (!run.attempt_count) {
    result.attempts = [];
    result.branch_name = null;
    result.artifacts = [];
    result.events = [{ id: 1, event_type: 'created', payload: {}, created_at: run.created_at }];
  }
  return result;
}
