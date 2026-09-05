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
  let view = $state<'tasks' | 'events' | 'backend'>('tasks');
  let pageIndex = $state(0);
  let eventPage = $state(0);
  let eventSearch = $state('');
  const pageSize = 6;
  let events = $derived(
    (data?.recent_events ?? []).filter((event) =>
      `${event.issue_title} ${event.project_slug} ${event.issue_number} ${eventLabel(event)}`
        .toLowerCase()
        .includes(eventSearch.trim().toLowerCase()),
    ),
  );
  let eventLastPage = $derived(Math.max(0, Math.ceil(events.length / pageSize) - 1));
  let currentEventPage = $derived(Math.min(eventPage, eventLastPage));
  let visibleEvents = $derived(
    events.slice(currentEventPage * pageSize, (currentEventPage + 1) * pageSize),
  );
  $effect(() => {
    void eventSearch;
    eventPage = 0;
  });
  let selection = $state<string | null>(null);
  let stage = $derived(selection ?? (compact ? 'sessions' : 'all'));
  let workerErrors = $derived(
    Object.values(data?.manager.workers ?? {}).filter((worker) => worker.state === 'error').length,
  );
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
        (stage === 'sessions'
          ? ['running', 'claimed'].includes(r.status) ||
            !!data?.manager.live_driver_run_ids.includes(r.run_id)
          : stage === 'attention'
            ? ['failed', 'parked', 'blocked_quota'].includes(r.status)
            : selectedStages?.includes(r.status)),
    ),
  );
  $effect(() => {
    void stage;
    pageIndex = 0;
  });
  let ordered = $derived(
    [...filtered].sort((a, b) => {
      const rank = (id: string, status: string) =>
        data?.manager.live_driver_run_ids.includes(id)
          ? 0
          : status === 'running'
            ? 1
            : status === 'claimed'
              ? 2
              : 3;
      return rank(a.run_id, a.status) - rank(b.run_id, b.status);
    }),
  );
  let lastPage = $derived(Math.max(0, Math.ceil(ordered.length / pageSize) - 1));
  let currentPage = $derived(Math.min(pageIndex, lastPage));
  let visible = $derived(
    compact
      ? ordered.slice(0, 3)
      : ordered.slice(currentPage * pageSize, (currentPage + 1) * pageSize),
  );
  let headline = $derived(
    !data
      ? 'Connecting to backend activity…'
      : stale
        ? 'Live updates interrupted.'
        : !data.manager.available
          ? 'Scheduler activity unavailable.'
          : executing
            ? `${executing} active ${executing === 1 ? 'session' : 'sessions'}`
            : working
              ? 'Running tasks need manager attention.'
              : 'No active sessions',
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
      <div>
        <h2>
          {compact || view === 'tasks' || stale || !data
            ? headline
            : view === 'events'
              ? 'Recent events'
              : 'Backend status'}
        </h2>
        {#if stale}<p>Showing the last received snapshot. Retrying automatically.</p>{/if}
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
  {#if !compact}<nav class="activity-views" aria-label="Activity views">
      <button aria-pressed={view === 'tasks'} onclick={() => (view = 'tasks')}
        ><Icon name="agent" size={18} />Tasks<span>{data?.active_runs_total ?? '—'}</span></button
      >
      <button aria-pressed={view === 'events'} onclick={() => (view = 'events')}
        ><Icon name="activity" size={18} />Events<span>{data?.recent_events.length ?? '—'}</span
        ></button
      >
      <button aria-pressed={view === 'backend'} onclick={() => (view = 'backend')}
        ><Icon name="cpu" size={18} />Backend{#if workerErrors}<span class="view-error"
            >{workerErrors} {workerErrors === 1 ? 'error' : 'errors'}</span
          >{/if}</button
      >
    </nav>{/if}
  {#if inspectError}<p class="activity-warning" role="alert">{inspectError}</p>{/if}
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
    {#if compact || view === 'tasks'}
      <div class="work-pipeline" aria-label="Task stages">
        {#each activityStages as item (item.label)}
          {@const count = item.statuses.reduce((n, s) => n + (data?.status_counts[s] ?? 0), 0)}
          <button
            class:chosen={stage === item.label}
            class:occupied={count > 0}
            aria-pressed={stage === item.label}
            onclick={() => (selection = stage === item.label ? null : item.label)}
          >
            <span class="pipeline-node"><Icon name={item.icon} size={18} /></span>
            <span class="pipeline-label">{item.label}<strong>{count}</strong></span>
          </button>
        {/each}
      </div>
      <div class="activity-scope">
        <button
          class="text-button"
          aria-pressed={stage === 'all'}
          onclick={() => (selection = 'all')}
          >{stage === 'all' ? 'Current tasks' : 'Show all current tasks'}<span
            >{data.active_runs_total}</span
          ></button
        >
        <div>
          {#if exceptions}<button
              class="exception-filter"
              aria-pressed={stage === 'attention'}
              onclick={() => (selection = stage === 'attention' ? null : 'attention')}
              ><Icon name="warning" size={13} />{exceptions} blocked or failed</button
            >{/if}<span>{data.status_counts.merged ?? 0} completed</span>
        </div>
      </div>
      <div class="session-columns" aria-hidden="true">
        <span>Task</span><span>Status</span><span>Last signal</span><span></span>
      </div>
      <div class="live-task-list">
        {#each visible as run (run.run_id)}
          {@const attended = data.manager.live_driver_run_ids.includes(run.run_id)}
          {@const expired =
            run.status === 'running' &&
            !!run.lease_expires_at &&
            Date.parse(run.lease_expires_at) < clock}
          <details class="session-row">
            <summary class="live-task">
              <span class="session-task"
                ><strong>{run.issue_title}</strong><span
                  >{run.project_slug} · #{run.issue_number}{#if run.provider}
                    · {run.provider}{/if}</span
                ></span
              >
              <span
                class="status status-{run.status}"
                class:signal-warning={expired || (run.status === 'running' && !attended)}
                ><i></i>{expired
                  ? 'Lease expired'
                  : run.status === 'running' && !attended
                    ? 'Unattended'
                    : statusLabels[run.status]}</span
              >
              <span class="session-signal" title={run.last_heartbeat_at ?? run.updated_at}
                >{run.status === 'running'
                  ? run.last_heartbeat_at
                    ? timeAgo(run.last_heartbeat_at, clock)
                    : 'No heartbeat'
                  : timeAgo(run.updated_at, clock)}</span
              >
              <span class="session-chevron"><Icon name="down" size={16} /></span>
            </summary>
            <div class="session-runtime">
              <div class="runtime-heading">
                <p>{waitReason(run, attended)}</p>
                <button
                  class="text-button"
                  disabled={!!inspecting}
                  onclick={() => inspect(run.run_id)}
                  >{inspecting === run.run_id ? 'Opening…' : 'Open task'}<Icon
                    name="arrow"
                    size={15}
                  /></button
                >
              </div>
              <dl>
                <div>
                  <dt>Backend status</dt>
                  <dd>{statusLabels[run.status]}</dd>
                </div>
                {#if run.status === 'running'}<div>
                    <dt>Heartbeat</dt>
                    <dd>{timeAgo(run.last_heartbeat_at, clock)}</dd>
                  </div>{/if}
                {#if ['queued', 'blocked_quota', 'failed'].includes(run.status)}<div>
                    <dt>Retry / scheduling</dt>
                    <dd>{nextCheck(run.next_attempt_at, clock)}</dd>
                  </div>{/if}
                {#if run.container_id}<div>
                    <dt>Container</dt>
                    <dd><code>{run.container_id}</code></dd>
                  </div>{/if}
                {#if run.attempt_started_at}<div>
                    <dt>Attempt started</dt>
                    <dd>{new Date(run.attempt_started_at).toLocaleString()}</dd>
                  </div>{/if}
                {#if run.lease_expires_at}<div>
                    <dt>Lease expires</dt>
                    <dd class:signal-warning={expired}>
                      {new Date(run.lease_expires_at).toLocaleString()}
                    </dd>
                  </div>{/if}
                {#if run.hard_deadline_at}<div>
                    <dt>Deadline</dt>
                    <dd>{new Date(run.hard_deadline_at).toLocaleString()}</dd>
                  </div>{/if}
              </dl>
            </div>
          </details>
        {:else}<p class="activity-empty">
            {stage === 'all' ? 'No open tasks.' : 'No tasks in this stage in the current snapshot.'}
          </p>{/each}
      </div>
      {#if !compact && ordered.length > pageSize}<div
          class="activity-pagination"
          aria-label="Task pages"
        >
          <span
            >{currentPage * pageSize + 1}–{Math.min((currentPage + 1) * pageSize, ordered.length)} of
            {ordered.length} loaded tasks</span
          >
          <div>
            <button
              class="button"
              aria-label="Previous tasks"
              disabled={currentPage === 0}
              onclick={() => (pageIndex = currentPage - 1)}>Previous</button
            ><button
              class="button"
              aria-label="Next tasks"
              disabled={currentPage >= lastPage}
              onclick={() => (pageIndex = currentPage + 1)}>Next</button
            >
          </div>
        </div>{/if}
      {#if compact && onexpand}<button class="activity-expand" onclick={onexpand}
          >View all activity{#if filtered.length > 3}
            · {filtered.length - 3} more tasks{/if}<Icon name="arrow" size={15} /></button
        >{/if}
      {#if !compact && data.active_runs.length < data.active_runs_total}<p
          class="activity-annotation"
        >
          Showing {data.active_runs.length} of {data.active_runs_total} open tasks. Stage counts cover
          the entire workspace. Use Agents to browse every task.
        </p>{/if}
    {/if}
    {#if compact || view === 'backend'}
      <details class="background-work" open={!compact}>
        <summary class="backend-summary"
          ><h3>Backend processes</h3>
          <span class:signal-warning={workerErrors > 0}
            >{!data.manager.available
              ? 'Unavailable'
              : workerErrors
                ? `${workerErrors} ${workerErrors === 1 ? 'error' : 'errors'}`
                : `${Object.keys(data.manager.workers).length} processes`}</span
          ><Icon name="down" size={16} /></summary
        >
        <div class="worker-list">
          {#each Object.entries(data.manager.workers) as [key, worker] (key)}
            <details class="worker" class:worker-error={worker.state === 'error'}>
              <summary
                ><strong>{workerNames[key] ?? humanize(key)}</strong><span class="worker-action"
                  >{worker.current_operation
                    ? humanize(worker.current_operation.kind)
                    : !data.manager.available
                      ? 'Unavailable'
                      : worker.state === 'error'
                        ? 'Retry pending'
                        : worker.state === 'idle'
                          ? 'Idle'
                          : nextCheck(worker.waiting_until, clock)}</span
                ><span class="worker-state"
                  >{stale
                    ? 'Last known'
                    : worker.state === 'running'
                      ? 'Working'
                      : worker.state === 'error'
                        ? 'Error'
                        : worker.state === 'waiting'
                          ? 'Waiting'
                          : 'Idle'}</span
                ><Icon name="down" size={15} /></summary
              >
              <dl>
                <div>
                  <dt>Last completed</dt>
                  <dd>{timeAgo(worker.last_completed_at, clock)}</dd>
                </div>
                {#if worker.last_error_at}<div>
                    <dt>Last error</dt>
                    <dd class="signal-warning">{timeAgo(worker.last_error_at, clock)}</dd>
                  </div>{/if}
                {#if worker.current_operation?.key}<div>
                    <dt>Target</dt>
                    <dd><code>{worker.current_operation.key}</code></dd>
                  </div>{/if}
                {#if worker.waiting_until}<div>
                    <dt>Next check</dt>
                    <dd>{new Date(worker.waiting_until).toLocaleString()}</dd>
                  </div>{/if}
              </dl>
            </details>
          {/each}
        </div>
      </details>
      {#if !compact}
        <details class="operation-details">
          <summary
            >Backend operation log <span
              >{data.manager.recent_operations.length} recent operations</span
            ></summary
          >
          <p class="activity-annotation">
            Operations from this manager process reset on restart. Recorded task events remain
            available in Events.
          </p>
          {#each data.manager.recent_operations as operation, index (index)}
            <div class="operation-row">
              <Icon name={operation.outcome === 'failed' ? 'warning' : 'check'} size={15} /><span
                ><strong>{humanize(operation.kind)}</strong><small
                  >{workerNames[operation.worker] ?? operation.worker} · {operation.key}</small
                ></span
              ><span class:signal-warning={operation.outcome === 'failed'}>{operation.outcome}</span
              ><time title={operation.completed_at}>{timeAgo(operation.completed_at, clock)}</time
              ><code>{Math.round(operation.duration_ms)} ms</code>
            </div>
          {:else}<p class="activity-annotation">
              No completed operations reported in this process.
            </p>{/each}
        </details>
      {/if}
    {/if}
    {#if !compact && view === 'events'}
      <div class="event-toolbar">
        <label class="event-search"
          ><Icon name="search" size={17} /><input
            aria-label="Search recorded events"
            placeholder="Search tasks, projects, or changes"
            bind:value={eventSearch}
          /></label
        ><span>Latest {data.recent_events.length} recorded events</span>
      </div>
      <div class="event-columns" aria-hidden="true">
        <span>Time</span><span>Task</span><span>Change</span>
      </div>
      <ol class="activity-event-list">
        {#each visibleEvents as event (event.id)}<li>
            <button onclick={() => inspect(event.run_id)} disabled={!!inspecting}>
              <time datetime={event.created_at} title={new Date(event.created_at).toLocaleString()}
                >{new Date(event.created_at).toLocaleTimeString([], {
                  hour: '2-digit',
                  minute: '2-digit',
                  second: '2-digit',
                })}</time
              >
              <span
                ><strong>{event.issue_title}</strong><small
                  >{event.project_slug} · #{event.issue_number}</small
                ></span
              >
              <span class="event-change">{eventLabel(event)}</span>
            </button>
          </li>{:else}<li class="activity-empty">
            {eventSearch ? 'No recorded events match this search.' : 'No recorded task events yet.'}
          </li>{/each}
      </ol>
      {#if events.length > pageSize}<div class="activity-pagination" aria-label="Event pages">
          <span
            >{currentEventPage * pageSize + 1}–{Math.min(
              (currentEventPage + 1) * pageSize,
              events.length,
            )} of {events.length} matching events</span
          >
          <div>
            <button
              class="button"
              aria-label="Previous events"
              disabled={currentEventPage === 0}
              onclick={() => (eventPage = currentEventPage - 1)}>Previous</button
            ><button
              class="button"
              aria-label="Next events"
              disabled={currentEventPage >= eventLastPage}
              onclick={() => (eventPage = currentEventPage + 1)}>Next</button
            >
          </div>
        </div>{/if}
    {/if}
  {:else if !error}<div class="activity-empty">
      <span class="spinner"></span>Waiting for the first activity snapshot…
    </div>{/if}
</section>

<style>
  .activity-monitor {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 20px;
    min-width: 0;
  }
  .activity-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
  }
  h2 {
    margin: 0;
    font-size: 22px;
    font-weight: 600;
    line-height: 1.35;
    letter-spacing: -0.02em;
  }
  h3 {
    margin: 0 0 14px;
    font-size: 16px;
    font-weight: 600;
  }
  .activity-title p {
    font-size: 14px;
    color: var(--muted);
    margin: 8px 0 0;
  }
  .activity-freshness {
    color: var(--muted);
    font-size: 13px;
    text-align: right;
    flex-shrink: 0;
  }
  .activity-freshness > span {
    display: flex;
    gap: 7px;
    align-items: center;
    justify-content: flex-end;
  }
  .activity-freshness small {
    display: block;
    font-size: 12px;
    margin-top: 4px;
  }
  .activity-freshness i {
    width: 6px;
    height: 6px;
    display: inline-block;
    border-radius: 50%;
    background: #64748b;
  }
  .activity-freshness i.live {
    background: var(--accent);
  }
  .stale,
  .signal-warning {
    color: #9a481d;
  }
  .work-pipeline {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    margin: 20px 0 16px;
    gap: 8px;
  }
  .work-pipeline button {
    display: flex;
    gap: 8px;
    align-items: center;
    justify-content: center;
    background: #f6f8fc;
    padding: 12px 7px;
    border: 1px solid transparent;
    border-radius: 8px;
  }
  .work-pipeline button:hover,
  .work-pipeline button.chosen {
    background: #eaf1ff;
    border-color: #bfd2f7;
  }
  .pipeline-node {
    display: inline-flex;
    color: var(--muted);
  }
  .occupied .pipeline-node {
    color: var(--accent);
  }
  .pipeline-label {
    display: flex;
    gap: 8px;
    font-size: 14px;
    color: var(--muted);
  }
  .pipeline-label strong {
    color: var(--text);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .activity-scope {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
    padding-bottom: 12px;
    color: var(--muted);
    font-size: 13px;
  }
  .activity-scope > div {
    display: flex;
    gap: 14px;
    align-items: center;
  }
  .activity-monitor .text-button {
    font-size: 14px;
  }
  .activity-scope .text-button span {
    margin-left: 5px;
    color: var(--muted);
  }
  .exception-filter {
    display: flex;
    align-items: center;
    gap: 6px;
    color: #94511d;
    background: #fff5e9;
    padding: 7px 9px;
    border: none;
    border-radius: 6px;
    font-size: 13px;
  }
  .exception-filter[aria-pressed='true'] {
    outline: 1px solid currentColor;
  }
  .session-columns,
  .live-task {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 115px 100px 16px;
    gap: 16px;
    align-items: center;
  }
  .session-columns {
    color: var(--muted);
    font-size: 12px;
    padding: 10px 0;
    border-bottom: 1px solid var(--border);
  }
  .session-row {
    border-bottom: 1px solid var(--border);
  }
  .live-task {
    list-style: none;
    padding: 14px 0;
    cursor: pointer;
  }
  .live-task::-webkit-details-marker {
    display: none;
  }
  .live-task:hover {
    background: #f5f8fe;
  }
  .session-task {
    min-width: 0;
  }
  .session-task strong {
    display: block;
    font-size: 15px;
    font-weight: 550;
    line-height: 1.45;
    overflow-wrap: anywhere;
  }
  .session-task > span {
    display: block;
    color: var(--muted);
    font-size: 13px;
    margin-top: 5px;
    line-height: 1.5;
  }
  .live-task .status {
    font-size: 13px;
    justify-self: start;
    white-space: normal;
    line-height: 1.4;
  }
  .session-signal {
    color: var(--muted);
    font-size: 13px;
    font-variant-numeric: tabular-nums;
  }
  .session-chevron {
    display: flex;
  }
  details[open] > summary .session-chevron {
    transform: rotate(180deg);
  }
  .session-runtime {
    padding: 0 0 20px;
  }
  .runtime-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 16px;
  }
  .runtime-heading p {
    font-size: 14px;
    margin: 0;
    line-height: 1.6;
  }
  .runtime-heading button {
    flex-shrink: 0;
  }
  dl {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px 24px;
    margin: 0;
    padding: 16px;
    background: #f6f8fc;
    border-radius: 8px;
  }
  dl > div {
    min-width: 0;
  }
  dt {
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 5px;
  }
  dd {
    font-size: 14px;
    margin: 0;
    line-height: 1.5;
    overflow-wrap: anywhere;
  }
  dd code {
    font-size: 13px;
  }
  .activity-expand {
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 10px;
    width: 100%;
    padding: 17px 0 0;
    background: none;
    border: 0;
    color: var(--accent);
    font-size: 14px;
  }
  .background-work {
    margin-top: 16px;
    border-top: 1px solid var(--border);
    padding-top: 14px;
  }
  .backend-summary {
    display: flex;
    align-items: center;
    gap: 12px;
    cursor: pointer;
    list-style: none;
  }
  .backend-summary::-webkit-details-marker {
    display: none;
  }
  .backend-summary h3 {
    margin: 0;
    flex: 1;
  }
  .backend-summary > span {
    font-size: 13px;
    color: var(--muted);
  }
  .backend-summary > span.signal-warning {
    color: #9a481d;
  }
  .worker-list {
    padding-top: 14px;
  }
  .worker {
    border-bottom: 1px solid var(--border);
  }
  .worker:last-child {
    border-bottom: 0;
  }
  .worker summary {
    list-style: none;
    display: grid;
    grid-template-columns: minmax(145px, 1fr) minmax(0, 1fr) 66px 16px;
    gap: 16px;
    align-items: center;
    padding: 13px 0;
    cursor: pointer;
    font-size: 13px;
  }
  .worker summary::-webkit-details-marker {
    display: none;
  }
  .worker summary:hover {
    background: #f5f8fe;
  }
  .worker summary strong {
    font-weight: 550;
  }
  .worker-action,
  .worker-state {
    color: var(--muted);
    overflow-wrap: anywhere;
  }
  .worker-state {
    text-align: right;
  }
  .worker-error .worker-action,
  .worker-error .worker-state {
    color: #9a481d;
  }
  .worker dl {
    margin: 0 0 14px;
  }
  .activity-event-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .activity-event-list li {
    border-bottom: 1px solid var(--border);
  }
  .event-dot {
    display: none;
  }
  .activity-event-list button {
    display: flex;
    justify-content: space-between;
    gap: 20px;
    width: 100%;
    padding: 15px 0;
    border: 0;
    background: none;
    text-align: left;
  }
  .activity-event-list button:hover {
    background: #f5f8fe;
  }
  .activity-event-list button > span {
    display: grid;
    gap: 5px;
  }
  .activity-event-list strong {
    font-size: 14px;
    font-weight: 550;
  }
  .activity-event-list small {
    font-size: 13px;
    color: var(--muted);
    line-height: 1.5;
    overflow-wrap: anywhere;
  }
  time {
    color: var(--muted);
    font-size: 13px;
    white-space: nowrap;
  }
  .activity-warning {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 14px;
    line-height: 1.6;
    padding: 14px;
    margin: 16px 0;
    color: #94511d;
    background: #fff5e9;
    border-radius: 8px;
  }
  .activity-warning > span {
    flex: 1;
  }
  .activity-empty {
    color: var(--muted);
    font-size: 14px;
    padding: 18px 0;
  }
  .activity-annotation {
    color: var(--muted);
    font-size: 13px;
    line-height: 1.7;
    margin: 16px 0 0;
  }
  .operation-details {
    margin-top: 16px;
    border-top: 1px solid var(--border);
    padding-top: 20px;
  }
  .operation-details > summary {
    font-size: 15px;
    cursor: pointer;
  }
  .operation-details > summary span {
    font-size: 13px;
    color: var(--muted);
    margin-left: 12px;
  }
  .operation-row {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 14px 0;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
  }
  .operation-row > span:first-of-type {
    flex: 1;
    min-width: 0;
  }
  .operation-row strong {
    font-weight: 550;
  }
  .operation-row small {
    display: block;
    font-size: 13px;
    color: var(--muted);
    margin-top: 5px;
    overflow-wrap: anywhere;
  }
  .operation-row code {
    font-size: 12px;
    color: var(--muted);
  }
  @media (max-width: 1100px) {
    .compact .pipeline-node {
      display: none;
    }
    .compact .session-columns,
    .compact .live-task {
      grid-template-columns: minmax(0, 1fr) 95px 75px 16px;
      gap: 10px;
    }
    .compact .worker summary {
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 16px;
      gap: 10px;
    }
    .compact .worker-state {
      display: none;
    }
  }
  @media (max-width: 760px) {
    .activity-monitor {
      padding: 18px 16px;
    }
    .activity-heading {
      align-items: flex-start;
      gap: 12px;
    }
    h2 {
      font-size: 20px;
    }
    .activity-freshness {
      font-size: 12px;
    }
    .activity-freshness small {
      max-width: 90px;
    }
    .work-pipeline {
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin: 16px 0;
    }
    .work-pipeline button {
      padding: 12px 8px;
    }
    .pipeline-node {
      display: none;
    }
    .pipeline-label {
      flex-direction: row;
      align-items: center;
      gap: 8px;
      font-size: 14px;
    }
    .pipeline-label strong {
      font-size: 15px;
    }
    .activity-scope {
      align-items: flex-start;
    }
    .activity-scope > div {
      flex-wrap: wrap;
      gap: 8px;
    }
    .session-columns {
      display: none;
    }
    .live-task,
    .compact .live-task {
      grid-template-columns: minmax(0, 1fr) auto 16px;
      gap: 10px;
    }
    .session-task {
      grid-column: 1 / 3;
    }
    .session-chevron {
      grid-column: 3;
      grid-row: 1 / 3;
    }
    .live-task .status {
      grid-column: 1;
    }
    .session-signal {
      grid-column: 2;
      text-align: right;
    }
    .runtime-heading {
      align-items: flex-start;
      flex-direction: column;
      gap: 10px;
    }
    dl {
      grid-template-columns: 1fr;
      gap: 14px;
    }
    .worker summary,
    .compact .worker summary {
      grid-template-columns: minmax(0, 1fr) 16px;
      gap: 6px 10px;
    }
    .worker summary strong {
      font-size: 14px;
    }
    .worker-action {
      grid-column: 1;
    }
    .worker summary :global(svg) {
      grid-column: 2;
      grid-row: 1 / 3;
    }
    .worker-state {
      display: none;
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

  .activity-views {
    display: flex;
    gap: 8px;
    margin: 22px 0 0;
    border-bottom: 1px solid var(--border);
  }
  .activity-views button {
    display: flex;
    align-items: center;
    gap: 9px;
    min-height: 48px;
    padding: 12px 16px;
    border: 0;
    border-bottom: 2px solid transparent;
    background: none;
    font-size: 15px;
    color: var(--muted);
  }
  .activity-views button[aria-pressed='true'] {
    border-bottom-color: var(--accent);
    color: var(--accent);
    background: #f4f7ff;
  }
  .activity-views button:hover {
    background: #f4f7ff;
  }
  .activity-views button span {
    font-size: 13px;
    color: var(--muted);
  }
  .activity-views .view-error {
    color: #9a481d;
  }
  .activity-pagination {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    margin-top: 16px;
    font-size: 13px;
    color: var(--muted);
  }
  .activity-pagination > div {
    display: flex;
    gap: 8px;
  }
  .activity-pagination .button {
    min-height: 40px;
    font-size: 13px;
  }
  .event-toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 20px;
    margin: 20px 0;
    font-size: 13px;
    color: var(--muted);
  }
  .event-search {
    display: flex;
    align-items: center;
    gap: 10px;
    flex: 1;
    max-width: 420px;
    padding: 0 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
  }
  .event-search input {
    border: 0;
    background: transparent;
    padding: 12px 0;
    width: 100%;
    font-size: 14px;
    outline-offset: 4px;
  }
  .event-columns,
  .activity-event-list button {
    display: grid;
    grid-template-columns: 90px minmax(0, 1fr) minmax(150px, 0.4fr);
    gap: 24px;
    align-items: center;
  }
  .event-columns {
    font-size: 13px;
    color: var(--muted);
    padding: 12px 0;
    border-bottom: 1px solid var(--border);
  }
  .activity-event-list button > .event-change {
    font-size: 14px;
    color: var(--accent);
  }
  @media (max-width: 760px) {
    .activity-views {
      gap: 0;
    }
    .activity-views button {
      justify-content: center;
      flex: 1;
      flex-wrap: wrap;
      gap: 5px;
      padding: 10px 7px;
      font-size: 14px;
    }
    .activity-views button :global(svg) {
      display: none;
    }
    .activity-views button span {
      font-size: 12px;
    }
    .event-toolbar {
      flex-wrap: wrap;
      gap: 10px;
    }
    .event-search {
      flex-basis: 100%;
    }
    .event-columns {
      display: none;
    }
    .activity-event-list button {
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
    }
    .activity-event-list button > span:first-of-type {
      grid-row: 1;
      grid-column: 1 / -1;
    }
    .activity-event-list button > time {
      grid-row: 2;
      grid-column: 2;
    }
    .activity-event-list button > .event-change {
      grid-row: 2;
      grid-column: 1;
    }
    .activity-pagination {
      flex-wrap: wrap;
      gap: 10px;
    }
  }
</style>
