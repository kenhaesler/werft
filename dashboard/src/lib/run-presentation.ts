import { statusLabels } from './format';
import { eventLabel, humanize } from './activity';
import type { RunDetail, RunStatus } from './types';

export const runExplanation: Record<
  RunStatus,
  { title: string; description: string; next: string }
> = {
  queued: {
    title: 'Waiting to start',
    description: 'This task is queued for an agent attempt.',
    next: 'The scheduler will check capacity and provider quota before starting an attempt.',
  },
  claimed: {
    title: 'Preparing the agent workspace',
    description: 'Werft has claimed this task and is preparing its execution environment.',
    next: 'Werft will start the coding agent once preparation succeeds.',
  },
  running: {
    title: 'Agent session in progress',
    description:
      'The backend reports an active execution attempt. Individual commands and edits are not streamed here.',
    next: 'Wait for the agent result. Werft then evaluates the result and any required checks or review.',
  },
  awaiting_ci: {
    title: 'Waiting for automated checks',
    description: 'This task is waiting for GitHub CI results.',
    next: 'Werft will evaluate the checks to determine whether the work can proceed.',
  },
  awaiting_review: {
    title: 'Ready for your review',
    description: 'This task needs your decision before it can proceed.',
    next: 'Inspect the evidence, then accept or reject the work using the actions below.',
  },
  merging: {
    title: 'Merge in progress',
    description: 'Werft is handling the repository merge and verification.',
    next: 'Wait for the merge result. A failure or blocker will be reported here.',
  },
  blocked_quota: {
    title: 'Waiting for provider quota',
    description: 'The task cannot proceed with the currently available provider quota.',
    next: 'Werft will reassess quota before another attempt can start.',
  },
  failed: {
    title: 'An attempt failed',
    description: 'The backend reported a failure. The task has not completed successfully.',
    next: 'Check the error and evidence for the cause. The scheduler determines whether a retry is available.',
  },
  parked: {
    title: 'Task paused for attention',
    description: 'Automatic work on this task is paused.',
    next: 'Resolve the reported reason, then requeue the task to let Werft try again.',
  },
  merged: {
    title: 'Work merged',
    description: 'The backend reports that this task was merged.',
    next: 'Review the pull request or collected evidence for the completed work.',
  },
  canceled: {
    title: 'Task canceled',
    description: 'This task has been canceled.',
    next: 'Recorded attempts and evidence remain available for inspection.',
  },
};

export function milestoneTitle(event: RunDetail['events'][number]): string {
  const known: Record<string, string> = {
    'run.claimed': 'Task assigned',
    'runner.started': 'Agent environment started',
    'agent.working': 'Agent work reported',
  };
  if (known[event.event_type]) return known[event.event_type];
  const to = event.payload.to ?? event.payload.to_status;
  if (event.event_type === 'status_changed' && typeof to === 'string') {
    return `Status changed to ${(statusLabels[to as RunStatus] ?? humanize(to)).toLowerCase()}`;
  }
  const phases: Record<string, string> = {
    claimed: 'Task assigned',
    container_died: 'Agent environment stopped',
    abandoned: 'Attempt abandoned',
  };
  if (typeof event.payload.phase === 'string' && phases[event.payload.phase])
    return phases[event.payload.phase];
  return eventLabel({
    ...event,
    run_id: '',
    project_slug: '',
    issue_number: 0,
    issue_title: '',
    run_status: 'queued',
    phase: typeof event.payload.phase === 'string' ? event.payload.phase : null,
    from_status: typeof event.payload.from_status === 'string' ? event.payload.from_status : null,
    to_status:
      typeof event.payload.to === 'string'
        ? event.payload.to
        : typeof event.payload.to_status === 'string'
          ? event.payload.to_status
          : null,
  }).replaceAll('.', ' ');
}

export function providerName(value: string | null | undefined): string {
  return value ? ({ claude: 'Claude', codex: 'Codex' }[value] ?? humanize(value)) : 'Not reported';
}
