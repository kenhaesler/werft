<script lang="ts">
  import { tick, untrack } from 'svelte';
  import Icon from './Icon.svelte';
  import { getSessionLog, getSessionRuntime, type SessionRuntime } from './session-client';

  const MAX_RETAINED_BYTES = 256 * 1024;
  let { runId, demo = false }: { runId: string; demo?: boolean } = $props();
  let runtime = $state<SessionRuntime | null>(null);
  let output = $state('');
  let generation = $state<string | undefined>();
  let offset = $state<number | undefined>();
  let unavailable = $state('');
  let error = $state('');
  let paused = $state(false);
  let following = $state(true);
  let logElement = $state<HTMLPreElement>();
  let activeRunId = '';
  $effect(() => {
    if (activeRunId !== runId) {
      activeRunId = runId;
      runtime = null;
      output = '';
      generation = undefined;
      offset = undefined;
      unavailable = '';
      error = '';
      following = true;
    }
    if (demo) {
      runtime = {
        generated_at: new Date().toISOString(),
        manager_available: true,
        attended: true,
        attempt_no: 1,
        run: {
          run_id: runId,
          status: 'running',
          provider: 'claude',
          container_id: 'preview-environment',
          attempt_started_at: new Date().toISOString(),
          last_heartbeat_at: new Date().toISOString(),
          lease_expires_at: null,
          hard_deadline_at: null,
          next_attempt_at: null,
          parked_reason: null,
          updated_at: new Date().toISOString(),
        },
      };
      output =
        'Sample session output\n\nThis preview illustrates retained raw agent output. It does not represent live commands or edits.\n';
      return;
    }
    const id = runId;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let inFlight = false;
    const append = async (content: string, reset: boolean) => {
      const previous = untrack(() => output);
      output = reset ? content : `${previous}${content}`;
      const updated = untrack(() => output);
      if (updated.length > MAX_RETAINED_BYTES)
        output = `[Earlier output omitted; keeping the latest ${MAX_RETAINED_BYTES / 1024} KB]\n${updated.slice(-MAX_RETAINED_BYTES)}`;
      if (untrack(() => following)) {
        await tick();
        logElement?.scrollTo({ top: logElement.scrollHeight });
      }
    };
    const load = async () => {
      try {
        const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)]);
        const cursor = untrack(() => ({ offset, generation }));
        const [nextRuntime, chunk] = await Promise.all([
          getSessionRuntime(id, signal),
          paused ? null : getSessionLog(id, cursor, signal),
        ]);
        if (controller.signal.aborted || runId !== id) return;
        runtime = nextRuntime;
        error = '';
        if (chunk) {
          if (!chunk.available)
            unavailable =
              chunk.reason === 'unsafe_output'
                ? 'Output is unavailable because it was marked unsafe.'
                : 'Retained output is not available for this session.';
          else {
            unavailable = '';
            generation = chunk.generation ?? undefined;
            offset = chunk.next_offset ?? undefined;
            await append(chunk.content, chunk.reset);
          }
        }
      } catch (cause) {
        if (!controller.signal.aborted)
          error = cause instanceof Error ? cause.message : 'Could not refresh the session.';
      }
    };
    const schedule = async () => {
      if (inFlight || controller.signal.aborted) return;
      inFlight = true;
      await load();
      inFlight = false;
      if (!controller.signal.aborted)
        timer = setTimeout(schedule, document.visibilityState === 'visible' ? 2_000 : 15_000);
    };
    const visible = () => {
      if (document.visibilityState === 'visible') {
        if (timer) clearTimeout(timer);
        void schedule();
      }
    };
    void schedule();
    document.addEventListener('visibilitychange', visible);
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
      document.removeEventListener('visibilitychange', visible);
    };
  });
  function onScroll() {
    if (logElement)
      following = logElement.scrollHeight - logElement.scrollTop - logElement.clientHeight < 40;
  }
</script>

<section class="session" aria-label="Live session">
  <div class="session-heading">
    <div>
      <h3>{demo ? 'Sample session output' : 'Session output'}</h3>
      <p>Retained raw agent output. Logs do not guarantee a command-by-command activity stream.</p>
    </div>
    <button class="text-button" onclick={() => (paused = !paused)}
      ><Icon name={paused ? 'play' : 'pause'} size={15} />{paused
        ? 'Resume follow'
        : 'Pause follow'}</button
    >
  </div>
  {#if error}<p class="state warning">
      Live session updates are interrupted: {error}. Retrying automatically.
    </p>{/if}
  {#if unavailable}<p class="state">{unavailable} Previously received output remains below.</p>{/if}
  <pre bind:this={logElement} onscroll={onScroll}>{output ||
      'Waiting for retained session output…'}</pre>
  <details>
    <summary>Runtime facts</summary>{#if runtime}<dl>
        <div>
          <dt>Manager</dt>
          <dd>
            {runtime.manager_available
              ? runtime.attended
                ? 'Attending this session'
                : 'Not attending this session'
              : 'Unavailable'}
          </dd>
        </div>
        <div>
          <dt>Session</dt>
          <dd>{runtime.attempt_no ? `#${runtime.attempt_no}` : 'Not reported'}</dd>
        </div>
        {#if runtime.run}<div>
            <dt>Provider</dt>
            <dd>{runtime.run.provider ?? 'Not reported'}</dd>
          </div>
          <div>
            <dt>Container</dt>
            <dd><code>{runtime.run.container_id ?? 'Not reported'}</code></dd>
          </div>
          <div>
            <dt>Last heartbeat</dt>
            <dd>
              {runtime.run.last_heartbeat_at
                ? new Date(runtime.run.last_heartbeat_at).toLocaleString()
                : 'Not reported'}
            </dd>
          </div>
          <div>
            <dt>Deadline</dt>
            <dd>
              {runtime.run.hard_deadline_at
                ? new Date(runtime.run.hard_deadline_at).toLocaleString()
                : 'Not reported'}
            </dd>
          </div>{/if}
      </dl>{:else}<p>Loading session runtime…</p>{/if}
  </details>
</section>

<style>
  .session {
    font-size: 14px;
    color: var(--text);
  }
  .session-heading {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    align-items: flex-start;
  }
  h3 {
    font-size: 16px;
    margin: 0;
  }
  p {
    color: var(--muted);
    line-height: 1.55;
    margin: 6px 0 0;
  }
  .text-button {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    min-height: 44px;
    padding: 10px 0;
    border: 0;
    background: none;
    color: var(--text-control);
    font-size: 14px;
    white-space: nowrap;
  }
  pre {
    max-height: 420px;
    min-height: 180px;
    overflow: auto;
    margin: 16px 0;
    padding: 14px;
    border-radius: 10px;
    background: var(--surface-input);
    color: var(--text);
    font-size: 14px;
    line-height: 1.55;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .state {
    padding: 12px 0;
    border-bottom: 1px solid var(--border);
  }
  .warning {
    color: var(--danger);
  }
  summary {
    min-height: 44px;
    display: flex;
    align-items: center;
    cursor: pointer;
    color: var(--text-control);
  }
  dl {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px;
    margin: 8px 0 0;
  }
  dt {
    color: var(--muted);
    font-size: 13px;
    margin-bottom: 4px;
  }
  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  code {
    font-size: 13px;
  }
  @media (max-width: 600px) {
    .session-heading,
    dl {
      grid-template-columns: 1fr;
      flex-direction: column;
    }
  }
</style>
