<script lang="ts">
  import { untrack } from 'svelte';
  import Icon from './Icon.svelte';
  import { api, downloadArtifact } from './api';
  import { demoDetail } from './demo';
  import { bytes, duration, relativeTime, safeExternalUrl, statusLabels } from './format';
  import type { RunDetail, RunSummary } from './types';
  let {
    run,
    demo,
    busy,
    message = '',
    onclose,
    onaction,
  }: {
    run: RunSummary;
    demo: boolean;
    busy: boolean;
    message?: string;
    onclose: () => void;
    onaction: (action: 'accept' | 'reject' | 'cancel' | 'requeue', run: RunSummary) => void;
  } = $props();
  let detail = $state<RunDetail | null>(null);
  let error = $state('');
  let downloadError = $state('');
  let downloading = $state('');
  let tab = $state('Timeline');
  let retry = $state(0);
  let syncError = $state('');
  let lastSuccessfulUpdate = $state('');
  const runId = $derived(run.id);
  $effect(() => {
    const id = runId;
    const initialRun = demo ? run : untrack(() => run);
    void retry;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let wake: (() => void) | undefined;
    detail = null;
    error = '';
    syncError = '';
    lastSuccessfulUpdate = '';
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
  const displayed = $derived(detail ?? run);
  async function download(path: string) {
    downloadError = '';
    downloading = path;
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
    } finally {
      downloading = '';
    }
  }
</script>

<div class="inspector-heading">
  <span><Icon name="agent" />Run workspace</span><button
    class="icon-button"
    aria-label="Close run details"
    onclick={onclose}><Icon name="close" /></button
  >
</div>
<div class="inspector-body">
  <div class="inspector-meta">
    <span class="status status-{displayed.status}"><i></i>{statusLabels[displayed.status]}</span
    ><span>{displayed.project_slug} / #{displayed.issue_number}</span>
  </div>
  <h2>{displayed.issue_title}</h2>
  <div class="detail-facts">
    <div>
      <span>Attempts</span><strong>{displayed.attempt_count} / {displayed.max_attempts}</strong>
    </div>
    <div><span>Last updated</span><strong>{relativeTime(displayed.updated_at)}</strong></div>
    <div>
      <span>Outcome</span><strong
        >{displayed.latest_outcome?.replaceAll('_', ' ') ?? 'In progress'}</strong
      >
    </div>
  </div>
  {#if displayed.parked_reason}<p class="notice warning">
      <Icon name="warning" />{displayed.parked_reason.replaceAll('_', ' ')}
    </p>{/if}
  {#if safeExternalUrl(displayed.pr_url)}<a
      class="button"
      href={safeExternalUrl(run.pr_url)}
      target="_blank"
      rel="noreferrer">View pull request #{displayed.pr_number}<Icon name="external" size={15} /></a
    >{/if}
  {#if detail?.branch_name}<div class="branch-line">
      <Icon name="branch" size={15} /><code>{detail.branch_name}</code>
    </div>{/if}
  <div class="tabs" aria-label="Run details">
    {#each ['Timeline', 'Evidence', 'Attempts'] as item (item)}<button
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
  {#if error}<div class="empty-state">
      <p role="alert">{error}</p>
      <button class="button" onclick={() => retry++}>Retry</button>
    </div>{:else if !detail}<div class="loading-state">
      <span class="spinner"></span>Loading run evidence…
    </div>{:else}
    {#if detail.error_message}<p class="notice warning">{detail.error_message}</p>{/if}
    {#if tab === 'Timeline'}<div class="event-timeline">
        {#each detail.events as event (event.id)}<div class="timeline-event">
            <span class="timeline-point"
              ><Icon
                name={event.event_type.includes('fail') ? 'warning' : 'check'}
                size={12}
              /></span
            >
            <div>
              <div class="timeline-title">
                <strong>{event.event_type.replaceAll('.', ' · ').replaceAll('_', ' ')}</strong
                ><small>{relativeTime(event.created_at)}</small>
              </div>
              {#if typeof event.payload.message === 'string'}<p>
                  {event.payload.message}
                </p>{:else if Object.keys(event.payload).length}<pre>{JSON.stringify(
                    event.payload,
                    null,
                    2,
                  )}</pre>{/if}
            </div>
          </div>{:else}<p class="muted">No events recorded yet.</p>{/each}
      </div>
      {#if detail.result}<details class="result-details">
          <summary>Structured result</summary>
          <pre>{JSON.stringify(detail.result, null, 2)}</pre>
        </details>{/if}
    {:else if tab === 'Evidence'}<p class="section-description">
        {demo
          ? 'Sample files for exploring the workspace.'
          : 'Collected files from this run. Downloads use your authenticated connection.'}
      </p>
      {#each detail.artifacts as artifact (artifact.path)}<button
          class="artifact-row"
          disabled={!!downloading}
          onclick={() => download(artifact.path)}
          ><Icon name="file" /><span
            ><strong>{artifact.path}</strong><small>{bytes(artifact.bytes)}</small></span
          ><Icon name={downloading === artifact.path ? 'clock' : 'download'} size={16} /></button
        >{:else}<div class="empty-state">
          <Icon name="file" size={28} />
          <h3>No evidence collected yet</h3>
          <p>Files appear when the manager collects this run’s outputs.</p>
        </div>{/each}{#if downloadError}<p class="notice warning" role="alert">
          {downloadError}
        </p>{/if}
    {:else}{#each detail.attempts as attempt (attempt.attempt_no)}<div class="attempt-row">
          <span class="attempt-number">{attempt.attempt_no}</span>
          <div>
            <strong>{attempt.provider}</strong><small
              >{attempt.outcome?.replaceAll('_', ' ') ?? 'Running'}</small
            >
          </div>
          <span
            >{attempt.duration_seconds !== null
              ? duration(attempt.duration_seconds)
              : 'In progress'}</span
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
