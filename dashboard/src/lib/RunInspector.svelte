<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import Icon from './Icon.svelte';
  import { api, downloadArtifact } from './api';
  import { loadArtifactMetadata } from './artifact-evidence';
  import ArtifactEvidence from './ArtifactEvidence.svelte';
  import LiveSession from './LiveSession.svelte';
  import Conversation from './Conversation.svelte';
  import RunProgress from './RunProgress.svelte';
  import ResultSummary from './ResultSummary.svelte';
  import { demoDetail } from './demo';
  import { duration, relativeTime, safeExternalUrl, statusLabels } from './format';
  import type { ActivitySnapshot, RunDetail, RunSummary } from './types';
  import { milestoneTitle, providerName, runExplanation } from './run-presentation';
  import { timeAgo } from './activity';
  let {
    run,
    demo,
    busy,
    message = '',
    activity = null,
    activityError = '',
    activityFetchedAt = 0,
    onclose,
    onaction,
  }: {
    run: RunSummary;
    demo: boolean;
    busy: boolean;
    message?: string;
    activity?: ActivitySnapshot | null;
    activityError?: string;
    activityFetchedAt?: number;
    onclose: () => void;
    onaction: (action: 'accept' | 'reject' | 'cancel' | 'requeue', run: RunSummary) => void;
  } = $props();
  let detail = $state<RunDetail | null>(null);
  let error = $state('');
  let downloadError = $state('');
  let tab = $state('Overview');
  let now = $state(Date.now());
  onMount(() => {
    const timer = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(timer);
  });
  let retry = $state(0);
  let syncError = $state('');
  let lastSuccessfulUpdate = $state('');
  let artifactMetadata = $state<import('./types').Artifact[] | null>(null);
  let artifactError = $state('');
  const runId = $derived(run.id);
  $effect(() => {
    const id = runId;
    const initialRun = demo ? run : untrack(() => run);
    void retry;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let wake: (() => void) | undefined;
    tab = 'Overview';
    detail = null;
    error = '';
    syncError = '';
    lastSuccessfulUpdate = '';
    artifactMetadata = null;
    artifactError = '';
    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        if (timer) clearTimeout(timer);
        wake?.();
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    const wait = (ms: number) =>
      new Promise<void>((resolve) => {
        timer = setTimeout(resolve, ms);
        wake = () => {
          if (timer) clearTimeout(timer);
          timer = undefined;
          wake = undefined;
          resolve();
        };
      });
    const refresh = async (initial = false) => {
      try {
        const data = demo
          ? demoDetail(initialRun)
          : await api<RunDetail>(`/runs/${id}`, {
              signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)]),
            });
        if (controller.signal.aborted) return;
        detail = data;
        if (!demo) {
          try {
            const metadata = await loadArtifactMetadata(
              id,
              AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)]),
            );
            if (controller.signal.aborted || runId !== id) return;
            artifactMetadata = metadata;
            artifactError = '';
          } catch (artifactFailure) {
            if (!controller.signal.aborted) {
              artifactError =
                artifactFailure instanceof Error
                  ? artifactFailure.message
                  : 'Could not load artifact integrity details.';
            }
          }
        }
        error = '';
        syncError = '';
        lastSuccessfulUpdate = new Date().toISOString();
      } catch (err) {
        if (controller.signal.aborted) return;
        if (initial) error = err instanceof Error ? err.message : 'Could not load this run.';
        else syncError = err instanceof Error ? err.message : 'Live update failed.';
      }
    };
    const poll = async () => {
      await refresh(true);
      while (!controller.signal.aborted && !demo) {
        await wait(document.visibilityState === 'visible' ? 3000 : 60_000);
        if (!controller.signal.aborted && document.visibilityState === 'visible') await refresh();
      }
    };
    void poll();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
      wake?.();
      wake = undefined;
      document.removeEventListener('visibilitychange', onVisibility);
    };
  });
  const displayed = $derived(
    !detail ||
      Date.parse(run.updated_at) > Date.parse(detail.updated_at) ||
      (run.updated_at === detail.updated_at && run.status !== detail.status)
      ? { ...detail, ...run }
      : detail,
  );
  const explanation = $derived(runExplanation[displayed.status]);
  const latestAttempt = $derived(
    detail?.attempts.slice().sort((a, b) => b.attempt_no - a.attempt_no)[0],
  );
  const failureBudget = $derived(`${displayed.attempt_count} of ${displayed.max_attempts} used`);
  const evidenceArtifacts = $derived(
    artifactMetadata ??
      detail?.artifacts.map((artifact) => ({ ...artifact, content_hash: null })) ??
      [],
  );
  const runtime = $derived(activity?.active_runs.find((item) => item.run_id === runId));
  const runtimeAvailable = $derived(
    !!activity?.manager.available &&
      (demo || (!activityError && activityFetchedAt > 0 && now - activityFetchedAt < 12_000)),
  );
  const attended = $derived(
    runtimeAvailable && !!activity?.manager.live_driver_run_ids.includes(runId),
  );
  const events = $derived(
    detail?.events
      .slice()
      .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id - a.id) ?? [],
  );
  const latestEvent = $derived(events[0]);
  const provider = $derived(
    providerName(
      runtimeAvailable && runtime?.status === displayed.status
        ? (runtime?.provider ?? latestAttempt?.provider)
        : latestAttempt?.provider,
    ),
  );
  async function download(path: string) {
    downloadError = '';
    try {
      if (demo) {
        const blob = new Blob(['Werft preview artifact. This is illustrative sample evidence.\n'], {
          type: 'text/plain',
        });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `sample-${path.split('/').pop()}`;
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } else await downloadArtifact(run.id, path);
    } catch {
      downloadError = `Could not download ${path}. Please retry.`;
    }
  }
</script>

<div class="inspector-heading">
  <span
    ><Icon name="agent" />Task details{#if demo}<span class="inspector-preview">Sample task</span
      >{/if}</span
  ><button class="icon-button" aria-label="Close run details" onclick={onclose}
    ><Icon name="close" /></button
  >
</div>
<div class="inspector-body">
  <div class="inspector-meta">
    <span class="status status-{displayed.status}"><i></i>{statusLabels[displayed.status]}</span
    ><span>{displayed.project_slug} / #{displayed.issue_number}</span>
  </div>
  <h2>{displayed.issue_title}</h2>
  <div class="tabs" aria-label="Run details">
    {#each ['Overview', 'Conversation', 'Session', 'Timeline', 'Evidence', 'Attempts'] as item (item)}<button
        class:active={tab === item}
        aria-pressed={tab === item}
        onclick={() => (tab = item)}
        >{item}{#if item === 'Evidence' && detail}<span>{detail.artifacts.length}</span
          >{/if}</button
      >{/each}
  </div>
  {#if syncError && detail}<p class="notice warning" role="status">
      Live updates interrupted: {syncError}. Retrying automatically. Last successful update{lastSuccessfulUpdate
        ? `: ${new Date(lastSuccessfulUpdate).toLocaleTimeString()}`
        : ''}.
    </p>{/if}
  {#if tab === 'Conversation'}
    <Conversation scope={run.id} {demo} />
  {:else if error}<div class="empty-state">
      <p role="alert">{error}</p>
      <button class="button" onclick={() => retry++}>Retry</button>
    </div>{:else if !detail}<div class="loading-state">
      <span class="spinner"></span>Loading run evidence…
    </div>{:else}
    {#if displayed.parked_reason}<p class="notice warning">
        <Icon name="warning" />{displayed.parked_reason.replaceAll('_', ' ')}
      </p>{/if}
    {#if detail.error_message}<p class="notice warning">{detail.error_message}</p>{/if}
    {#if tab === 'Overview'}
      <RunProgress status={displayed.status} />
      <section class="task-current" aria-label="Current task state">
        <h3>{explanation.title}</h3>
        <p>{explanation.description}</p>
        <div class="task-next">
          <strong>Next step</strong>
          <p>{explanation.next}</p>
        </div>
        {#if safeExternalUrl(displayed.pr_url)}<a
            class="button"
            href={safeExternalUrl(displayed.pr_url)}
            target="_blank"
            rel="noreferrer"
            >View pull request #{displayed.pr_number}<Icon name="external" size={15} /></a
          >{/if}
      </section>
      <section class="task-assignment" aria-label="Agent assignment">
        <div class="assignment-heading">
          <Icon name="agent" size={22} />
          <div>
            <h3>
              {latestAttempt
                ? provider === 'Not reported'
                  ? 'Agent provider not reported'
                  : `${provider} agent`
                : displayed.attempt_count
                  ? 'Session details unavailable'
                  : 'No agent assigned yet'}
            </h3>
            <p>
              {latestAttempt
                ? `${latestAttempt.ended_at ? 'Last session' : 'Current session'} #${latestAttempt.attempt_no}`
                : displayed.attempt_count
                  ? 'The backend has not returned attempt details.'
                  : 'An agent will be assigned when an attempt starts.'}
            </p>
          </div>
        </div>
        {#if latestAttempt}<p class="assignment-definition">
            An agent session is one attempt to complete this task in an isolated environment on your
            machine.
          </p>{/if}
        <p class="assignment-definition">
          <strong>Failure budget: {failureBudget}.</strong> Requeue starts a fresh failure budget while
          retaining earlier sessions.
        </p>
        {#if displayed.status === 'running'}
          <div class="assignment-signal" class:signal-warning={!attended}>
            <Icon name={attended ? 'activity' : 'warning'} size={17} /><span
              >{!runtimeAvailable
                ? 'Live session monitoring unavailable'
                : attended
                  ? 'Manager is monitoring this session'
                  : 'Manager is not currently monitoring this session'}</span
            >
          </div>
          {#if runtimeAvailable && runtime?.last_heartbeat_at}<p class="assignment-heartbeat">
              Last session heartbeat: {timeAgo(
                runtime.last_heartbeat_at,
                demo && activity ? Date.parse(activity.generated_at) : now,
              )}
            </p>{/if}
        {/if}
      </section>
      {#if latestEvent}<section class="task-latest" aria-label="Latest recorded update">
          <div>
            <h3>Latest recorded update</h3>
            <time
              datetime={latestEvent.created_at}
              title={new Date(latestEvent.created_at).toLocaleString()}
              >{relativeTime(latestEvent.created_at)}</time
            >
          </div>
          <strong>{milestoneTitle(latestEvent)}</strong>
          {#if typeof latestEvent.payload.message === 'string'}<p>
              {latestEvent.payload.message}
            </p>{/if}
          <button class="text-button" onclick={() => (tab = 'Timeline')}
            >View timeline<Icon name="arrow" size={15} /></button
          >
        </section>{/if}
      <details class="task-technical">
        <summary>Technical details</summary>
        <dl>
          <div>
            <dt>Task ID</dt>
            <dd><code>{displayed.id}</code></dd>
          </div>
          <div>
            <dt>Branch</dt>
            <dd><code>{detail.branch_name ?? 'Not created yet'}</code></dd>
          </div>
          <div>
            <dt>Last task update</dt>
            <dd>{new Date(displayed.updated_at).toLocaleString()}</dd>
          </div>
          <div>
            <dt>Last attempt outcome</dt>
            <dd>{displayed.latest_outcome?.replaceAll('_', ' ') ?? 'No outcome reported'}</dd>
          </div>
          {#if detail.base_sha}<div>
              <dt>Base commit</dt>
              <dd><code>{detail.base_sha}</code></dd>
            </div>{/if}
          {#if detail.merge_commit_sha}<div>
              <dt>Merge commit</dt>
              <dd><code>{detail.merge_commit_sha}</code></dd>
            </div>{/if}
          {#if runtimeAvailable && runtime?.status === displayed.status}<div>
              <dt>Agent container</dt>
              <dd><code>{runtime.container_id ?? 'Not reported'}</code></dd>
            </div>
            <div>
              <dt>Session deadline</dt>
              <dd>
                {runtime.hard_deadline_at
                  ? new Date(runtime.hard_deadline_at).toLocaleString()
                  : 'Not reported'}
              </dd>
            </div>{/if}
        </dl>
      </details>
    {:else if tab === 'Session'}<LiveSession runId={run.id} {demo} />
    {:else if tab === 'Timeline'}<p class="section-description">
        Recorded milestones, newest first. Open event details for the backend payload.
      </p>
      <div class="event-timeline">
        {#each events as event (event.id)}<div class="timeline-event">
            <span class="timeline-point"
              ><Icon
                name={event.event_type.includes('fail') ? 'warning' : 'activity'}
                size={14}
              /></span
            >
            <div>
              <div class="timeline-title">
                <strong>{milestoneTitle(event)}</strong><time
                  datetime={event.created_at}
                  title={new Date(event.created_at).toLocaleString()}
                  >{relativeTime(event.created_at)}</time
                >
              </div>
              {#if typeof event.payload.message === 'string'}<p>{event.payload.message}</p>{/if}
              <details class="event-payload">
                <summary>Event details</summary><code>{event.event_type}</code>
                <p>{new Date(event.created_at).toLocaleString()}</p>
                <pre>{JSON.stringify(event.payload, null, 2)}</pre>
              </details>
            </div>
          </div>{:else}<p class="muted">No events recorded yet.</p>{/each}
      </div>
      {#if detail.result}<ResultSummary result={detail.result} />{/if}
    {:else if tab === 'Evidence'}<p class="section-description">
        {demo
          ? 'Sample files for exploring the workspace.'
          : 'Collected files from this run. Downloads use your authenticated connection.'}
      </p>
      {#if artifactError}<p class="notice warning" role="status">
          Artifact integrity details are unavailable: {artifactError}. Downloads still work.
        </p>{/if}
      {#if evidenceArtifacts.length}<ArtifactEvidence
          runId={run.id}
          artifacts={evidenceArtifacts}
          {demo}
          ondownload={download}
        />{:else}<div class="empty-state">
          <Icon name="file" size={28} />
          <h3>No evidence collected yet</h3>
          <p>Files appear when the manager collects this run’s outputs.</p>
        </div>{/if}{#if downloadError}<p class="notice warning" role="alert">
          {downloadError}
        </p>{/if}
    {:else}<p class="section-description">
        Each attempt is a separate agent session. Earlier results remain available after a retry.
      </p>
      <p class="assignment-definition">
        <strong>Failure budget: {failureBudget}.</strong> Session numbers are lifetime records and can
        be higher than the current budget after a requeue.
      </p>
      {#each detail.attempts as attempt (attempt.attempt_no)}<div class="attempt-row">
          <span class="attempt-number">{attempt.attempt_no}</span>
          <div>
            <strong>Session #{attempt.attempt_no} · {providerName(attempt.provider)}</strong><small
              >{attempt.outcome?.replaceAll('_', ' ') ??
                (attempt.ended_at ? 'No outcome reported' : 'No outcome yet')}</small
            ><small
              >{new Date(attempt.started_at).toLocaleString()}{attempt.ended_at
                ? ` → ${new Date(attempt.ended_at).toLocaleString()}`
                : ''}</small
            >
          </div>
          <span
            >{attempt.duration_seconds !== null
              ? duration(attempt.duration_seconds)
              : attempt.ended_at
                ? 'Duration unavailable'
                : 'Not finished'}</span
          >
        </div>{:else}<p class="muted empty-inline">
          No attempts yet. This run is waiting to be dispatched.
        </p>{/each}{/if}
  {/if}
</div>
<div class="inspector-actions">
  {#if message}<p class="muted" role="status">{message}</p>{/if}
  {#if displayed.status === 'awaiting_review'}<button
      class="button primary"
      disabled={busy}
      onclick={() => onaction('accept', run)}><Icon name="check" size={16} />Accept work</button
    ><button class="button" disabled={busy} onclick={() => onaction('reject', run)}>Reject</button
    >{/if}{#if displayed.status === 'parked'}<button
      class="button primary"
      disabled={busy}
      onclick={() => onaction('requeue', run)}><Icon name="refresh" size={16} />Requeue run</button
    >{/if}{#if !['merged', 'canceled'].includes(displayed.status)}<button
      class="button danger"
      disabled={busy}
      onclick={() => onaction('cancel', run)}><Icon name="stop" size={14} />Cancel run</button
    >{:else}<span class="muted">This run is {statusLabels[displayed.status].toLowerCase()}.</span
    >{/if}{#if demo}<small>Actions affect this preview only.</small>{/if}
</div>
