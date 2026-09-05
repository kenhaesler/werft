<script lang="ts">
  import Icon from './Icon.svelte';
  import { ApiError, api } from './api';
  import type { Project } from './types';

  let {
    project,
    demo = false,
    onupdated,
    onrefresh,
  }: {
    project: Project;
    demo?: boolean;
    onupdated: (project: Project) => void;
    onrefresh?: () => void;
  } = $props();

  let busy = $state(false);
  let error = $state('');
  let errorStatus = $state<number | null>(null);
  let history = $state<
    { id: number; event_type: string; payload: Record<string, unknown>; created_at: string }[]
  >([]);
  let historyTotal = $state(0);
  let historyOffset = $state(0);
  let historyLoading = $state(false);
  let historyError = $state('');

  const lifecycleLabel = (value: string) =>
    value === 'oracle_gated' ? 'Oracle-gated' : value === 'bootstrap' ? 'Bootstrap' : value;

  function githubUrl(item: Project): string {
    return `https://github.com/${encodeURIComponent(item.owner)}/${encodeURIComponent(item.repo)}`;
  }

  function formatDate(value: string | null): string {
    if (!value) return 'Not recorded';
    const date = new Date(value);
    return Number.isNaN(date.valueOf())
      ? value
      : date.toLocaleString(undefined, { dateStyle: 'medium' });
  }

  function explainError(reason: unknown): string {
    if (reason instanceof ApiError) {
      errorStatus = reason.status;
      if (reason.status === 401)
        return 'Manager authorization is unavailable. Connect with a valid token, then retry.';
      if (reason.status === 409)
        return (
          reason.message ||
          'The project changed while this repair was in progress. Refresh and try again.'
        );
      return reason.message || `Lifecycle repair failed (HTTP ${reason.status}).`;
    }
    errorStatus = null;
    return reason instanceof Error
      ? reason.message
      : 'Lifecycle repair failed. Check the manager connection and retry.';
  }

  async function changeLifecycle(to: 'bootstrap' | 'oracle_gated') {
    if (busy || to === project.lifecycle) return;
    error = '';
    errorStatus = null;
    busy = true;
    try {
      const updated = demo
        ? { ...project, lifecycle: to }
        : await api<Project>(`/projects/${encodeURIComponent(project.id)}/flip`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ to }),
          });
      onupdated(updated);
    } catch (reason) {
      error = explainError(reason);
    } finally {
      busy = false;
    }
  }

  async function loadHistory(event?: Event) {
    if (demo || (event && !(event.currentTarget as HTMLDetailsElement).open) || historyLoading)
      return;
    historyLoading = true;
    historyError = '';
    try {
      const result = await api<{ total: number; events: typeof history }>(
        `/projects/${encodeURIComponent(project.id)}/events?limit=10&offset=${historyOffset}`,
      );
      history = result.events;
      historyTotal = result.total;
    } catch (reason) {
      historyError =
        reason instanceof Error
          ? reason.message
          : 'Project history is unavailable. Retry by reopening this section.';
    } finally {
      historyLoading = false;
    }
  }

  function historyPage(delta: number) {
    historyOffset = Math.max(0, historyOffset + delta * 10);
    void loadHistory();
  }
</script>

<details class="project-controls" ontoggle={loadHistory}>
  <summary>Project settings <Icon name="chevron" size={17} /></summary>
  <div class="settings-body">
    <div class="heading-row">
      <h2>{project.owner}/{project.repo}</h2>
      <span class="lifecycle" class:oracle={project.lifecycle === 'oracle_gated'}
        >{lifecycleLabel(project.lifecycle)}</span
      >
    </div>
    {#if demo}<p class="sample-label">SAMPLE ONLY · changes stay in this preview</p>{/if}
    <p class="lifecycle-explainer">
      {project.lifecycle === 'bootstrap' ? 'Human review' : 'CI checked'} ·
      {project.lifecycle === 'bootstrap'
        ? 'operators approve merges while CI is being proven.'
        : 'green werft-oracle checks gate merges.'}
    </p>
    <dl>
      <div>
        <dt>Lifecycle</dt>
        <dd>{lifecycleLabel(project.lifecycle)}</dd>
      </div>
      <div>
        <dt>Onboarded</dt>
        <dd>{formatDate(project.onboarded_at)}</dd>
      </div>
      <div>
        <dt>Created</dt>
        <dd>{formatDate(project.created_at)}</dd>
      </div>
      <div>
        <dt>Repository</dt>
        <dd>
          <a href={githubUrl(project)} target="_blank" rel="noreferrer"
            >Open on GitHub <Icon name="external" size={14} /></a
          >
        </dd>
      </div>
    </dl>
    <div class="repair">
      <div class="repair-copy">
        <h3>Manual lifecycle repair</h3>
        <p>Changing this state also changes protection on the <code>unattended</code> branch.</p>
      </div>
      <div class="actions" aria-label="Lifecycle repair actions">
        <button
          class="button secondary"
          disabled={busy || project.lifecycle === 'bootstrap'}
          onclick={() => changeLifecycle('bootstrap')}
        >
          {busy && project.lifecycle !== 'bootstrap' ? 'Repairing…' : 'Set bootstrap'}
        </button>
        <button
          class="button primary"
          disabled={busy || project.lifecycle === 'oracle_gated'}
          onclick={() => changeLifecycle('oracle_gated')}
        >
          {busy && project.lifecycle !== 'oracle_gated' ? 'Repairing…' : 'Set oracle-gated'}
        </button>
      </div>
      <p class="impact">
        <Icon name="shield" size={16} /> Bootstrap keeps partial protection while CI is being proven.
        Oracle-gated requires the <code>werft-oracle</code> check before merge.
      </p>
    </div>

    {#if !demo}
      <details class="history">
        <summary>Lifecycle history <Icon name="chevron" size={16} /></summary>
        {#if historyLoading}<p class="muted">Loading project history…</p>
        {:else if historyError}<p class="history-error" role="alert">{historyError}</p>
        {:else if !historyTotal}<p class="muted">No lifecycle events recorded yet.</p>
        {:else}<ol>
            {#each history as event (event.id)}<li>
                <div>
                  <strong>{event.event_type.replaceAll('_', ' ')}</strong><time
                    datetime={event.created_at}>{formatDate(event.created_at)}</time
                  >
                </div>
                <details>
                  <summary>View event payload</summary>
                  <pre>{JSON.stringify(event.payload, null, 2)}</pre>
                </details>
              </li>{/each}
          </ol>
          <div class="history-footer">
            <span
              >{historyOffset + 1}–{Math.min(historyOffset + 10, historyTotal)} of {historyTotal}</span
            ><span
              ><button
                class="history-button"
                disabled={historyLoading || historyOffset === 0}
                onclick={() => historyPage(-1)}>Previous</button
              ><button
                class="history-button"
                disabled={historyLoading || historyOffset + 10 >= historyTotal}
                onclick={() => historyPage(1)}>Next</button
              ></span
            >
          </div>
        {/if}
      </details>
    {/if}

    {#if error}
      <div class="error" role="alert">
        <Icon name="warning" size={17} />
        <div>
          <strong>{errorStatus === 409 ? 'Project changed' : 'Could not repair lifecycle'}</strong
          ><span>{error}</span>{#if errorStatus === 409 && onrefresh}<button
              class="retry"
              onclick={onrefresh}>Refresh project</button
            >{/if}
        </div>
      </div>
    {/if}
  </div>
</details>

<style>
  .project-controls {
    background: var(--panel);
    border: 1px solid #dce5f0;
    border-radius: 12px;
    color: var(--text);
    padding: 16px 20px;
    font:
      14px/1.5 'Geist Variable',
      sans-serif;
  }
  .settings-body {
    padding-top: 17px;
  }
  .heading-row,
  .repair,
  .actions,
  summary,
  .error,
  .error > div {
    display: flex;
    align-items: center;
  }
  .heading-row {
    justify-content: space-between;
    gap: 18px;
  }
  .sample-label {
    color: var(--amber);
    font-size: 12px;
    font-weight: 650;
    letter-spacing: 0.04em;
    margin: 4px 0 0;
  }
  .lifecycle-explainer {
    color: var(--muted);
    margin: 6px 0 0;
  }
  h2,
  h3 {
    margin: 0;
    color: var(--text);
  }
  h2 {
    font-size: 18px;
    font-weight: 600;
  }
  h3 {
    font-size: 15px;
  }
  .lifecycle {
    border: 1px solid #b9cbe3;
    border-radius: 999px;
    color: var(--text-control);
    padding: 5px 10px;
    font-weight: 600;
    white-space: nowrap;
  }
  .lifecycle.oracle {
    color: var(--accent);
    border-color: var(--accent-border);
    background: #eef4ff;
  }
  .project-controls > summary {
    font-size: 15px;
  }
  summary {
    cursor: pointer;
    gap: 8px;
    font-weight: 600;
    min-height: 28px;
    list-style: none;
  }
  summary::-webkit-details-marker {
    display: none;
  }
  summary :global(svg) {
    transition: transform 180ms ease;
  }
  details[open] summary :global(svg) {
    transform: rotate(90deg);
  }
  dl {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 13px 24px;
    margin: 16px 0 4px;
  }
  dl div {
    min-width: 0;
  }
  dt {
    color: var(--dim);
    font-size: 13px;
  }
  dd {
    margin: 2px 0 0;
    font-size: 14px;
    font-weight: 560;
    overflow-wrap: anywhere;
  }
  a {
    align-items: center;
    color: var(--accent);
    display: inline-flex;
    gap: 5px;
    text-decoration: none;
  }
  a:hover {
    text-decoration: underline;
  }
  .repair {
    border-top: 1px solid #e6edf5;
    flex-wrap: wrap;
    gap: 14px 20px;
    justify-content: space-between;
    margin-top: 18px;
    padding-top: 18px;
  }
  .repair-copy p {
    color: var(--muted);
    margin: 3px 0 0;
  }
  .history {
    border-top: 1px solid #e6edf5;
    margin-top: 18px;
    padding-top: 14px;
  }
  .history ol {
    list-style: none;
    margin: 14px 0 0;
    padding: 0;
  }
  .history li {
    border-bottom: 1px solid #edf1f6;
    padding: 9px 0;
  }
  .history li > div,
  .history-footer {
    align-items: center;
    display: flex;
    gap: 12px;
    justify-content: space-between;
  }
  .history time,
  .muted {
    color: var(--dim);
    font-size: 13px;
  }
  .history li details {
    margin-top: 4px;
  }
  .history li details summary {
    color: #52657e;
    font-size: 13px;
    min-height: 24px;
  }
  pre {
    background: #f7f9fc;
    border: 1px solid #e6edf5;
    border-radius: 7px;
    font:
      12px/1.45 'Cascadia Code',
      monospace;
    margin: 8px 0 0;
    overflow: auto;
    padding: 9px;
    white-space: pre-wrap;
  }
  .history-footer {
    color: var(--dim);
    font-size: 13px;
    margin-top: 12px;
  }
  .history-button {
    background: var(--surface-raised);
    border: 1px solid #b9cbe3;
    border-radius: 6px;
    color: var(--text-control);
    cursor: pointer;
    min-height: 34px;
    margin-left: 6px;
    padding: 5px 9px;
  }
  .history-button:disabled {
    cursor: default;
    opacity: 0.5;
  }
  .history-error {
    color: var(--danger);
    font-size: 14px;
  }
  .actions {
    flex-wrap: wrap;
    gap: 8px;
  }
  .button,
  .retry {
    border-radius: 7px;
    cursor: pointer;
    font:
      600 14px 'Geist Variable',
      sans-serif;
    min-height: 44px;
    padding: 9px 14px;
  }
  .button {
    border: 1px solid #b9cbe3;
  }
  .button:focus-visible,
  .retry:focus-visible,
  summary:focus-visible,
  a:focus-visible {
    outline: 2px solid #2463eb;
    outline-offset: 3px;
  }
  .button.primary {
    background: #2463eb;
    border-color: var(--accent);
    color: var(--accent-ink);
  }
  .button.primary:hover {
    background: #1d50c4;
  }
  .button.secondary {
    background: var(--surface-raised);
    color: var(--text-control);
  }
  .button.secondary:hover {
    background: #eef4ff;
  }
  .button:disabled {
    cursor: wait;
    opacity: 0.55;
  }
  .impact {
    align-items: flex-start;
    color: var(--muted);
    display: flex;
    flex-basis: 100%;
    gap: 8px;
    margin: 0;
  }
  code {
    font:
      13px 'Cascadia Code',
      monospace;
  }
  .error {
    align-items: flex-start;
    background: var(--danger-soft);
    border: 1px solid #efc7bd;
    border-radius: 9px;
    color: var(--danger);
    gap: 9px;
    margin-top: 16px;
    padding: 12px;
  }
  .error > div {
    align-items: flex-start;
    flex-direction: column;
    gap: 2px;
  }
  .error span {
    color: var(--danger);
  }
  .retry {
    background: transparent;
    border: 0;
    color: var(--danger);
    min-height: 30px;
    padding: 2px 0;
    text-decoration: underline;
  }
  @media (max-width: 560px) {
    dl {
      grid-template-columns: 1fr;
    }
    .heading-row {
      align-items: flex-start;
      flex-direction: column;
      gap: 10px;
    }
    .actions {
      width: 100%;
    }
    .button {
      flex: 1;
    }
  }
</style>
