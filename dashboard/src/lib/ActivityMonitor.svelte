<script lang="ts">
  import { onMount } from 'svelte';
  import Icon from './Icon.svelte';
  import {
    activityStages,
    eventLabel,
    humanize,
    nextCheck,
    timeAgo,
    waitReason,
    workerNames,
  } from './activity';
  import { statusLabels } from './format';
  import type { ActivitySnapshot, RunStatus } from './types';

  let {
    data,
    error = '',
    fetchedAt = 0,
    demo = false,
    compact = false,
    oninspect,
    onrefresh,
    onexpand,
  }: {
    data: ActivitySnapshot | null;
    error?: string;
    fetchedAt?: number;
    demo?: boolean;
    compact?: boolean;
    oninspect: (id: string) => Promise<void>;
    onrefresh: () => void;
    onexpand?: () => void;
  } = $props();
  let now = $state(Date.now());
  let stage = $state('all');
  let inspecting = $state('');
  let inspectError = $state('');
  let stale = $derived(!demo && (!!error || (fetchedAt > 0 && now - fetchedAt > 12_000)));
  let clock = $derived(
    data ? Date.parse(data.generated_at) + (demo ? 0 : Math.max(0, now - fetchedAt)) : now,
  );
  let executing = $derived(data?.manager.live_driver_run_ids.length ?? 0);
  let working = $derived(data?.status_counts.running ?? 0);
  let exceptions = $derived(
    ['failed', 'parked', 'blocked_quota'].reduce(
      (n, s) => n + (data?.status_counts[s as RunStatus] ?? 0),
      0,
    ),
  );
  let selectedStages = $derived(activityStages.find((s) => s.label === stage)?.statuses);
  let filtered = $derived(
    (data?.active_runs ?? []).filter(
      (r) =>
        stage === 'all' ||
        (stage === 'attention'
          ? ['failed', 'parked', 'blocked_quota'].includes(r.status)
          : selectedStages?.includes(r.status)),
    ),
  );
  let visible = $derived(compact ? filtered.slice(0, 2) : filtered);
  let headline = $derived(
    !data
      ? 'Connecting to backend activity…'
      : stale
        ? 'Live updates interrupted.'
        : !data.manager.available
          ? 'Scheduler activity unavailable.'
          : executing
            ? `${executing === 1 ? 'One agent session is' : `${executing} agent sessions are`} active.`
            : working
              ? 'Running tasks need manager attention.'
              : 'Werft is watching for the next step.',
  );

  onMount(() => {
    const timer = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(timer);
  });
  async function inspect(id: string) {
    if (inspecting) return;
    inspecting = id;
    inspectError = '';
    try {
      await oninspect(id);
    } catch {
      inspectError = 'Could not open this task. Check your connection and try again.';
    } finally {
      inspecting = '';
    }
  }
</script>

<section class="activity-monitor" class:compact aria-label="Backend activity">
  <div class="activity-heading">
    <div class="activity-title">
      <span class="activity-symbol" class:watching={!executing || stale}
        ><Icon name="activity" size={25} /></span
      >
      <div>
        <h2>{headline}</h2>
        <p>
          {demo
            ? 'Sample activity · connect your manager to follow real work.'
            : stale
              ? 'Showing the last received snapshot. Retrying automatically.'
              : 'Tasks, background checks, and VM activity in one place.'}
        </p>
      </div>
    </div>
    <div class="activity-freshness" class:stale>
      <span
        ><i class:live={!demo && !stale && !!data}></i>{demo
          ? 'Preview'
          : stale
            ? 'Updates delayed'
            : data
              ? 'Connected'
              : 'Connecting'}</span
      >
      {#if !demo && fetchedAt}<small title={new Date(fetchedAt).toLocaleString()}
          >Received {timeAgo(new Date(fetchedAt).toISOString(), now)}</small
        >{/if}
    </div>
  </div>
  {#if error}<div class="activity-warning" role="status">
      <Icon name="warning" size={16} /><span>{error}</span><button
        class="text-button"
        onclick={onrefresh}>Retry</button
      >
    </div>{/if}
  {#if data}
    {#if !data.manager.available}<p class="activity-warning">
        <Icon name="pause" size={16} />{data.manager.unavailable_reason === 'github_not_configured'
          ? 'GitHub is not configured. Connect GitHub in the manager configuration to start the scheduler.'
          : data.manager.unavailable_reason === 'stopped'
            ? 'The scheduler has stopped. Check the manager process and restart it to resume work.'
            : 'The manager is not reporting scheduler activity. Check its GitHub configuration and startup logs.'}
      </p>{/if}
    <div class="work-pipeline" aria-label="Task stages">
      {#each activityStages as item (item.label)}
        {@const count = item.statuses.reduce((n, s) => n + (data?.status_counts[s] ?? 0), 0)}
        <button
          class:chosen={stage === item.label}
          class:occupied={count > 0}
          aria-pressed={stage === item.label}
          onclick={() => (stage = stage === item.label ? 'all' : item.label)}
        >
          <span class="pipeline-node"><Icon name={item.icon} size={18} /></span>
          <span class="pipeline-label">{item.label}<strong>{count}</strong></span>
        </button>
      {/each}
    </div>
    <div class="activity-scope">
      <button class="text-button" aria-pressed={stage === 'all'} onclick={() => (stage = 'all')}
        >{stage === 'all' ? 'Current tasks' : 'Show all current tasks'}<span
          >{data.active_runs_total}</span
        ></button
      >
      <div>
        {#if exceptions}<button
            class="exception-filter"
            aria-pressed={stage === 'attention'}
            onclick={() => (stage = stage === 'attention' ? 'all' : 'attention')}
            ><Icon name="warning" size={13} />{exceptions} blocked or failed</button
          >{/if}<span>{data.status_counts.merged ?? 0} completed</span>
      </div>
    </div>
    <div class="live-task-list">
      {#each visible as run (run.run_id)}
        {@const attended = data.manager.live_driver_run_ids.includes(run.run_id)}
        <button class="live-task" disabled={!!inspecting} onclick={() => inspect(run.run_id)}>
          <span class="live-task-icon"
            ><Icon
              name={run.status === 'running'
                ? 'terminal'
                : run.status === 'awaiting_ci'
                  ? 'shield'
                  : 'clock'}
              size={19}
            /></span
          >
          <span class="live-task-content"
            ><span class="live-task-top"
              ><strong>{run.issue_title}</strong><span class="status status-{run.status}"
                ><i></i>{statusLabels[run.status]}</span
              ></span
            >
            <span class="live-task-project"
              >{run.project_slug} · #{run.issue_number}{#if run.provider}
                · {run.provider}{/if}</span
            >
            <span class="live-task-step">{waitReason(run, attended)}</span>
            <span class="live-task-telemetry">
              {#if run.status === 'running'}<span
                  class:heartbeat-expired={!!run.lease_expires_at &&
                    Date.parse(run.lease_expires_at) < clock}
                  >Heartbeat {timeAgo(
                    run.last_heartbeat_at,
                    clock,
                  )}{#if run.lease_expires_at && Date.parse(run.lease_expires_at) < clock}
                    · lease expired{/if}</span
                >
              {:else if ['queued', 'blocked_quota', 'failed'].includes(run.status)}<span
                  >{nextCheck(run.next_attempt_at, clock)}</span
                >
              {:else}<span>Updated {timeAgo(run.updated_at, clock)}</span>{/if}
              {#if run.container_id}<code title={run.container_id}
                  >VM {run.container_id.slice(0, 12)}</code
                >{/if}
              {#if !compact && run.hard_deadline_at}<span
                  title={new Date(run.hard_deadline_at).toLocaleString()}
                  >Deadline {new Date(run.hard_deadline_at).toLocaleTimeString()}</span
                >{/if}
              {#if !compact && run.attempt_started_at}<span
                  title={new Date(run.attempt_started_at).toLocaleString()}
                  >Attempt started {timeAgo(run.attempt_started_at, clock)}</span
                >{/if}
              {#if !compact && run.lease_expires_at}<span
                  title={new Date(run.lease_expires_at).toLocaleString()}
                  >Lease until {new Date(run.lease_expires_at).toLocaleTimeString()}</span
                >{/if}
            </span>
          </span><Icon name={inspecting === run.run_id ? 'clock' : 'chevron'} size={16} />
        </button>
      {:else}<div class="activity-empty">
          <Icon name="check" size={21} /><span
            >{stage === 'all'
              ? 'No open tasks. The next accepted issue will appear here.'
              : 'No tasks in this stage in the current snapshot.'}</span
          >
        </div>{/each}
    </div>
    {#if inspectError}<p class="activity-warning" role="alert">{inspectError}</p>{/if}
    {#if compact && onexpand}<button class="activity-expand" onclick={onexpand}
        >Open live activity{#if filtered.length > 2}
          · {filtered.length - 2} more tasks{/if}<Icon name="arrow" size={15} /></button
      >{/if}
    {#if !compact && data.active_runs.length < data.active_runs_total}<p
        class="activity-annotation"
      >
        Showing {data.active_runs.length} of {data.active_runs_total} open tasks. Stage counts cover the
        entire workspace. Use Agent workspace to browse every task.
      </p>{/if}

    <div class="background-work">
      <h3>Behind the scenes</h3>
      <div class="worker-list">
        {#each Object.entries(data.manager.workers) as [key, worker] (key)}
          <div class="worker" class:worker-error={worker.state === 'error'}>
            <div>
              <i class:busy={worker.state === 'running' && !stale && !demo}></i><strong
                >{workerNames[key] ?? humanize(key)}</strong
              ><span
                >{!data.manager.available
                  ? 'Unavailable'
                  : stale
                    ? 'Last known'
                    : worker.state === 'running'
                      ? 'Working'
                      : worker.state === 'error'
                        ? 'Retry pending'
                        : worker.state === 'idle'
                          ? 'Idle'
                          : 'Watching'}</span
              >
            </div>
            <p title={worker.current_operation?.key}>
              {worker.current_operation
                ? humanize(worker.current_operation.kind)
                : worker.state === 'error'
                  ? 'A check failed; the loop will retry.'
                  : !data.manager.available
                    ? 'No scheduler connected'
                    : worker.state === 'idle'
                      ? 'Waiting for first pass'
                      : nextCheck(worker.waiting_until, clock)}
            </p>
            <small title={worker.last_completed_at ?? undefined}
              >Last completed {timeAgo(worker.last_completed_at, clock)}</small
            >
            {#if worker.last_error_at}<small class="worker-error-time"
                >Last error {timeAgo(worker.last_error_at, clock)}</small
              >{/if}
          </div>
        {/each}
      </div>
    </div>
    <div class="activity-event-heading">
      <h3>Latest events</h3>
      <span>{compact ? 'Recorded milestones' : 'Latest 25 recorded milestones'}</span>
    </div>
    <ol class="activity-event-list">
      {#each data.recent_events.slice(0, compact ? 1 : 25) as event (event.id)}
        <li>
          <span class="event-dot"></span><button
            onclick={() => inspect(event.run_id)}
            disabled={!!inspecting}
            ><span
              ><strong>{eventLabel(event)}</strong><span>{event.issue_title}</span><small
                >{event.project_slug} · #{event.issue_number}</small
              ></span
            ><time datetime={event.created_at} title={new Date(event.created_at).toLocaleString()}
              >{timeAgo(event.created_at, clock)}</time
            ></button
          >
        </li>
      {:else}<li class="activity-empty">No recorded task events yet.</li>{/each}
    </ol>
    {#if !compact}<details class="operation-details">
        <summary
          >Backend operation log <span
            >{data.manager.recent_operations.length} recent operations</span
          ></summary
        >
        <p class="activity-annotation">
          Operations from this manager process. Task milestones above remain available after a
          restart.
        </p>
        {#each data.manager.recent_operations as operation, index (index)}
          <div class="operation-row">
            <Icon name={operation.outcome === 'failed' ? 'warning' : 'check'} size={15} /><span
              ><strong>{humanize(operation.kind)}</strong><small
                >{workerNames[operation.worker] ?? operation.worker} · {operation.key}</small
              ></span
            ><span class:heartbeat-expired={operation.outcome === 'failed'}
              >{operation.outcome}</span
            ><time title={operation.completed_at}>{timeAgo(operation.completed_at, clock)}</time
            ><code>{Math.round(operation.duration_ms)} ms</code>
          </div>
        {:else}<p class="activity-annotation">
            No completed operations reported in this process.
          </p>{/each}
      </details>{/if}
    <p class="activity-annotation activity-source">
      {demo
        ? 'Illustrative snapshot. No background work is running in preview.'
        : 'Refreshes every 3 seconds while visible. Milestones and runtime health; agent transcripts are available in task evidence.'}
    </p>
  {:else if !error}<div class="activity-empty">
      <span class="spinner"></span>Waiting for the first activity snapshot…
    </div>{/if}
</section>

<style>
  .activity-monitor {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 26px;
    min-width: 0;
  }
  .activity-heading,
  .activity-title {
    display: flex;
    align-items: center;
    gap: 14px;
  }
  .activity-heading {
    justify-content: space-between;
    align-items: flex-start;
    gap: 20px;
  }
  .activity-symbol {
    color: var(--accent);
    background: #edf3ff;
    border-radius: 12px;
    padding: 12px;
  }
  .activity-symbol.watching {
    color: #52657e;
    background: #f1f5fa;
  }
  h2 {
    font-size: 22px;
    line-height: 1.3;
    font-weight: 600;
    letter-spacing: -0.025em;
    margin: 0;
    text-wrap: balance;
  }
  .activity-title p {
    color: var(--muted);
    margin: 7px 0 0;
    font-size: 12px;
    line-height: 1.6;
  }
  .activity-freshness {
    text-align: right;
    flex-shrink: 0;
    font-size: 11px;
    color: var(--muted);
  }
  .activity-freshness > span {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 6px;
    font-weight: 600;
  }
  .activity-freshness small {
    display: block;
    margin-top: 5px;
    font-size: 10px;
  }
  .activity-freshness i,
  .worker i {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #64748b;
    display: inline-block;
    flex-shrink: 0;
  }
  .activity-freshness i.live,
  .worker i.busy {
    background: var(--accent);
  }
  .stale,
  .heartbeat-expired,
  .worker-error-time {
    color: #a34d20;
  }
  .work-pipeline {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    margin: 30px 0 22px;
  }
  .work-pipeline button {
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 9px;
    background: transparent;
    border: 0;
    padding: 4px 0;
    border-radius: 8px;
  }
  .work-pipeline button::before {
    content: '';
    position: absolute;
    height: 1px;
    background: var(--border);
    left: -50%;
    right: 50%;
    top: 22px;
  }
  .work-pipeline button:first-child::before {
    display: none;
  }
  .pipeline-node {
    position: relative;
    z-index: 1;
    display: grid;
    place-items: center;
    width: 38px;
    height: 38px;
    border-radius: 50%;
    border: 1px solid var(--border);
    background: white;
    color: var(--muted);
  }
  .occupied .pipeline-node {
    background: #eef4ff;
    color: var(--accent);
    border-color: #b8ccf5;
  }
  .chosen .pipeline-node {
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }
  .work-pipeline button:hover {
    background: #f5f8fe;
  }
  .pipeline-label {
    display: flex;
    gap: 7px;
    font-size: 12px;
    color: var(--muted);
  }
  .pipeline-label strong {
    color: var(--text);
    font-variant-numeric: tabular-nums;
    font-weight: 650;
  }
  .activity-scope,
  .activity-scope > div {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    font-size: 11px;
    color: var(--muted);
  }
  .activity-scope {
    padding-bottom: 12px;
  }
  .activity-scope .text-button {
    font-size: 12px;
    color: var(--text);
  }
  .activity-scope .text-button span {
    color: var(--muted);
    margin-left: 7px;
  }
  .exception-filter {
    display: flex;
    gap: 5px;
    align-items: center;
    background: #fff5e9;
    color: #94511d;
    border: 0;
    padding: 5px 7px;
    border-radius: 5px;
    font-size: 11px;
  }
  .exception-filter[aria-pressed='true'] {
    outline: 1px solid currentColor;
  }
  .live-task-list {
    border-top: 1px solid var(--border);
  }
  .live-task {
    display: flex;
    align-items: center;
    text-align: left;
    gap: 13px;
    width: 100%;
    border: 0;
    border-bottom: 1px solid var(--border);
    padding: 17px 0;
    background: none;
  }
  .live-task:hover {
    background: #f5f8fe;
  }
  .live-task-icon {
    align-self: flex-start;
    margin-top: 2px;
    color: var(--accent);
    background: #eef4ff;
    padding: 9px;
    border-radius: 9px;
  }
  .live-task-content {
    display: block;
    min-width: 0;
    flex: 1;
  }
  .live-task-top {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    justify-content: space-between;
  }
  .live-task-top > strong {
    font-size: 13px;
    font-weight: 600;
    line-height: 1.55;
    overflow-wrap: anywhere;
  }
  .live-task-top .status {
    flex-shrink: 0;
  }
  .live-task-project,
  .live-task-step {
    display: block;
    line-height: 1.5;
    font-size: 11px;
    color: var(--muted);
    margin-top: 4px;
  }
  .live-task-step {
    color: var(--text);
    margin-top: 10px;
    font-size: 12px;
  }
  .live-task-telemetry {
    display: flex;
    flex-wrap: wrap;
    gap: 5px 14px;
    font-size: 10px;
    line-height: 1.5;
    margin-top: 5px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .live-task-telemetry code {
    font-size: 10px;
  }
  .activity-expand {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    border: none;
    background: none;
    color: var(--accent);
    font-size: 12px;
    padding: 14px 0 0;
    width: 100%;
  }
  h3 {
    font-size: 13px;
    font-weight: 600;
    margin: 0;
  }
  .background-work {
    margin-top: 28px;
  }
  .worker-list {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 16px;
    margin-top: 13px;
  }
  .worker {
    min-width: 0;
  }
  .worker > div {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 5px;
    font-size: 11px;
  }
  .worker strong {
    font-weight: 550;
  }
  .worker > div > span {
    color: var(--muted);
    font-size: 10px;
  }
  .worker p {
    font-size: 11px;
    margin: 7px 0 3px;
    line-height: 1.5;
    color: var(--muted);
    overflow-wrap: anywhere;
  }
  .worker small {
    font-size: 10px;
    color: var(--muted);
    display: block;
    line-height: 1.5;
  }
  .worker.worker-error p,
  .worker small.worker-error-time {
    color: #a34d20;
  }
  .activity-event-heading {
    display: flex;
    justify-content: space-between;
    gap: 15px;
    align-items: center;
    margin-top: 28px;
    margin-bottom: 13px;
  }
  .activity-event-heading > span {
    font-size: 10px;
    color: var(--muted);
  }
  .activity-event-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .activity-event-list li {
    display: flex;
    position: relative;
    gap: 13px;
  }
  .event-dot {
    width: 7px;
    height: 7px;
    flex-shrink: 0;
    margin-top: 10px;
    border: 1px solid #88a6d1;
    background: white;
    border-radius: 50%;
  }
  .activity-event-list li:not(:last-child)::before {
    content: '';
    position: absolute;
    left: 3px;
    width: 1px;
    top: 18px;
    bottom: 0;
    background: var(--border);
  }
  .activity-event-list button {
    display: flex;
    flex: 1;
    min-width: 0;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
    border: 0;
    background: none;
    padding: 4px 0 15px;
    text-align: left;
  }
  .activity-event-list button:hover strong {
    color: var(--accent);
  }
  .activity-event-list button > span {
    display: grid;
    gap: 4px;
    min-width: 0;
  }
  .activity-event-list strong {
    font-size: 12px;
    font-weight: 550;
  }
  .activity-event-list button span span {
    font-size: 11px;
    color: var(--muted);
    overflow-wrap: anywhere;
  }
  .activity-event-list small {
    font-size: 10px;
    color: var(--dim);
  }
  time {
    font-size: 10px;
    color: var(--muted);
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
  .activity-warning {
    display: flex;
    align-items: center;
    gap: 9px;
    font-size: 12px;
    line-height: 1.6;
    color: #94511d;
    background: #fff5e9;
    padding: 12px;
    margin: 16px 0 0;
    border-radius: 8px;
  }
  .activity-warning > span {
    flex: 1;
  }
  .activity-empty {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 26px 0;
    color: var(--muted);
    font-size: 12px;
  }
  .activity-annotation {
    font-size: 11px;
    color: var(--muted);
    line-height: 1.7;
    margin: 12px 0;
  }
  .activity-source {
    border-top: 1px solid var(--border);
    padding-top: 13px;
    margin-bottom: 0;
    font-size: 10px;
  }
  .operation-details {
    margin-top: 18px;
    border-top: 1px solid var(--border);
    padding-top: 18px;
  }
  .operation-details summary {
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
  }
  .operation-details summary span {
    margin-left: 8px;
    font-size: 11px;
    color: var(--muted);
    font-weight: 400;
  }
  .operation-row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 11px 0;
    border-top: 1px solid var(--border);
    font-size: 11px;
  }
  .operation-row > span:first-of-type {
    flex: 1;
    min-width: 0;
  }
  .operation-row strong {
    font-weight: 500;
  }
  .operation-row small {
    display: block;
    color: var(--muted);
    margin-top: 5px;
    overflow-wrap: anywhere;
  }
  .operation-row code {
    color: var(--muted);
    font-size: 10px;
  }
  @media (min-width: 761px) {
    .compact h2 {
      font-size: 20px;
    }
    .compact .activity-heading {
      flex-wrap: wrap;
      gap: 10px;
    }
    .compact .activity-freshness {
      display: flex;
      gap: 8px;
      align-items: center;
      margin-left: 63px;
    }
    .compact .activity-freshness small {
      margin: 0;
    }
  }
  @media (max-width: 760px) {
    .activity-monitor {
      padding: 18px 15px;
    }
    .activity-heading {
      flex-direction: column;
      gap: 12px;
    }
    .activity-title {
      gap: 10px;
      align-items: flex-start;
    }
    .activity-symbol {
      padding: 8px;
      border-radius: 9px;
    }
    h2 {
      font-size: 20px;
    }
    .activity-title p {
      font-size: 11px;
    }
    .activity-freshness {
      display: flex;
      gap: 8px;
      align-items: center;
      padding-left: 51px;
    }
    .activity-freshness small {
      margin: 0;
    }
    .work-pipeline {
      margin: 23px 0 20px;
    }
    .pipeline-label {
      gap: 4px;
      font-size: 10px;
    }
    .pipeline-node {
      width: 32px;
      height: 32px;
    }
    .work-pipeline button::before {
      top: 19px;
    }
    .activity-scope {
      align-items: flex-start;
      gap: 6px;
    }
    .activity-scope > div {
      flex-direction: column;
      align-items: flex-end;
      gap: 6px;
    }
    .activity-scope .text-button {
      font-size: 11px;
    }
    .live-task {
      gap: 8px;
    }
    .live-task-icon {
      display: none;
    }
    .live-task-top {
      flex-direction: column-reverse;
      gap: 6px;
    }
    .live-task-top > strong {
      font-size: 13px;
    }
    .worker-list {
      grid-template-columns: 1fr;
      gap: 12px;
    }
    .worker {
      border-bottom: 1px solid var(--border);
      padding-bottom: 10px;
    }
    .worker:last-child {
      border: 0;
      padding-bottom: 0;
    }
    .worker > div {
      font-size: 12px;
    }
    .worker > div > span {
      margin-left: auto;
    }
    .worker small {
      display: inline;
      margin-right: 8px;
    }
    .operation-row {
      flex-wrap: wrap;
      gap: 8px;
    }
    .operation-row > span:first-of-type {
      flex-basis: calc(100% - 30px);
    }
    .operation-row time {
      margin-left: auto;
    }
  }
</style>
