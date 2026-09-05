import type { ActivityRun, ActivitySnapshot, RunStatus, RunSummary } from './types';
import { statusLabels } from './format';

export const activityStages: { label: string; icon: string; statuses: RunStatus[] }[] = [
  { label: 'Queue', icon: 'clock', statuses: ['queued', 'claimed'] },
  { label: 'Working', icon: 'terminal', statuses: ['running'] },
  { label: 'Verify', icon: 'shield', statuses: ['awaiting_ci'] },
  { label: 'Review', icon: 'review', statuses: ['awaiting_review'] },
  { label: 'Merge', icon: 'branch', statuses: ['merging'] },
];

export const workerNames: Record<string, string> = {
  tick: 'Task orchestration',
  issues: 'GitHub sync',
  checks: 'CI & merge checks',
};
export const humanize = (value: string) => value.replaceAll('_', ' ');
export function timeAgo(value: string | null, now: number): string {
  if (!value) return 'Not reported';
  const seconds = Math.max(0, Math.floor((now - Date.parse(value)) / 1000));
  if (seconds < 5) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
export function nextCheck(value: string | null, now: number): string {
  if (!value) return 'No check scheduled';
  const seconds = Math.ceil((Date.parse(value) - now) / 1000);
  if (seconds <= 0) return 'Due on the next pass';
  if (seconds < 60) return `Next check in ${seconds}s`;
  if (seconds < 3600) return `Next check in ${Math.ceil(seconds / 60)}m`;
  return `Next check at ${new Date(value).toLocaleString()}`;
}
export function waitReason(run: ActivityRun, attended: boolean): string {
  switch (run.status) {
    case 'running':
      return attended
        ? 'Manager is attending this agent session'
        : 'Run marked running · manager is not attending it';
    case 'claimed':
      return 'Preparing the workspace and agent environment';
    case 'queued':
      return 'Waiting for the scheduler and available capacity';
    case 'awaiting_ci':
      return 'Waiting for GitHub checks to finish';
    case 'awaiting_review':
      return 'Waiting for your review decision';
    case 'blocked_quota':
      return 'Waiting for provider quota to recover';
    case 'merging':
      return 'Merge verification and repository update';
    case 'parked':
      return run.parked_reason ? humanize(run.parked_reason) : 'Parked · operator action required';
    case 'failed':
      return 'Attempt failed · inspect evidence for the next step';
    default:
      return statusLabels[run.status];
  }
}
export function eventLabel(
  event: Pick<ActivitySnapshot['recent_events'][number], 'to_status' | 'phase' | 'event_type'>,
): string {
  if (event.to_status)
    return statusLabels[event.to_status as RunStatus] ?? humanize(event.to_status);
  const phases: Record<string, string> = {
    workspace_ready: 'Workspace prepared',
    container_started: 'Agent environment started',
    token_reminted: 'Agent credentials renewed',
  };
  if (event.phase) return phases[event.phase] ?? humanize(event.phase);
  return (
    {
      created: 'Task received',
      ci_observed: 'CI result received',
      cleanup: 'Environment cleanup',
      alert: 'Attention requested',
      dispatch: 'Dispatch updated',
      status_changed: 'Status changed',
    }[event.event_type] ?? humanize(event.event_type)
  );
}

/** Static illustrative telemetry, recomputed only when preview runs change. */
export function previewActivity(runs: RunSummary[]): ActivitySnapshot {
  const now = Date.now();
  const at = (seconds: number) => new Date(now + seconds * 1000).toISOString();
  return {
    generated_at: at(0),
    manager: {
      available: true,
      started_at: at(-3600),
      workers: Object.fromEntries(
        ['tick', 'issues', 'checks'].map((key, i) => [
          key,
          {
            state: 'waiting' as const,
            current_operation: null,
            last_started_at: at(-4 - i * 8),
            last_completed_at: at(-3 - i * 8),
            last_error_at: null,
            waiting_until: at(12 + i * 15),
          },
        ]),
      ),
      recent_operations: [],
      live_driver_run_ids: runs.filter((r) => r.status === 'running').map((r) => r.id),
    },
    status_counts: runs.reduce<ActivitySnapshot['status_counts']>((counts, r) => {
      counts[r.status] = (counts[r.status] ?? 0) + 1;
      return counts;
    }, {}),
    recent_events: [...runs]
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
      .slice(0, 8)
      .map((r, i) => ({
        id: i,
        run_id: r.id,
        project_slug: r.project_slug,
        issue_number: r.issue_number,
        issue_title: r.issue_title,
        run_status: r.status,
        event_type: 'status_changed',
        phase: null,
        from_status: null,
        to_status: r.status,
        created_at: r.updated_at,
      })),
    active_runs: runs
      .filter((r) => !['merged', 'canceled'].includes(r.status))
      .map((r) => ({
        run_id: r.id,
        project_slug: r.project_slug,
        issue_number: r.issue_number,
        issue_title: r.issue_title,
        status: r.status,
        provider: r.status === 'running' ? 'claude' : null,
        container_id: r.status === 'running' ? 'preview-environment' : null,
        attempt_started_at: r.created_at,
        last_heartbeat_at: r.status === 'running' ? at(-4) : null,
        lease_expires_at: r.status === 'running' ? at(116) : null,
        hard_deadline_at: r.status === 'running' ? at(3600) : null,
        next_attempt_at: at(30),
        parked_reason: r.parked_reason,
        updated_at: r.updated_at,
      })),
    active_runs_total: runs.filter((r) => !['merged', 'canceled'].includes(r.status)).length,
  };
}
