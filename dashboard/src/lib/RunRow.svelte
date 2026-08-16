<script lang="ts">
  import { api, ApiError } from './api';
  import type { Artifact, ArtifactsResponse, RunSummary } from './types';

  interface Props {
    run: RunSummary;
    onAccept?: () => void;
    onReject?: () => void;
    onCancel?: () => void;
    onRequeue?: () => void;
  }

  let { run, onAccept, onReject, onCancel, onRequeue }: Props = $props();

  const TERMINAL_STATES = new Set(['merged', 'canceled']);

  let isTerminal = $derived(TERMINAL_STATES.has(run.status));

  // The artifact listing needs the bearer header (`/api/v1/...` requires
  // it), so this can't be a plain `<a href>` — that would 401 and, worse,
  // would mean a token-bearing URL if it ever were made to work (binding
  // B4/B7 ruling: no tokens in URLs). Fetched on demand via the shared
  // `api()` helper, which attaches `Authorization` from stored state.
  let artifacts = $state<Artifact[] | null>(null);
  let artifactsError = $state<string | null>(null);
  let artifactsLoading = $state(false);

  async function loadArtifacts(): Promise<void> {
    artifactsLoading = true;
    artifactsError = null;
    try {
      const response = await api<ArtifactsResponse>(`/runs/${run.id}/artifacts`);
      artifacts = response.artifacts;
    } catch (err) {
      artifacts = null;
      artifactsError = err instanceof ApiError ? `failed to load (${err.status})` : 'failed to load';
    } finally {
      artifactsLoading = false;
    }
  }
</script>

<tr class="run-row">
  <td class="run-row__state">
    <span class="badge badge--{run.status}">{run.status}</span>
  </td>
  <td class="run-row__project">{run.project_slug}</td>
  <td class="run-row__issue">#{run.issue_number} {run.issue_title}</td>
  <td class="run-row__attempts">{run.attempt_count}/{run.max_attempts}</td>
  <td class="run-row__outcome">{run.latest_outcome ?? '—'}</td>
  <td class="run-row__parked">{run.parked_reason ?? ''}</td>
  <td class="run-row__pr">
    {#if run.pr_url}
      <a href={run.pr_url} target="_blank" rel="noopener noreferrer">
        PR{run.pr_number != null ? ` #${run.pr_number}` : ''}
      </a>
    {/if}
  </td>
  <td class="run-row__artifacts">
    <button type="button" onclick={loadArtifacts}>Artifacts</button>
    {#if artifactsLoading}
      <span class="run-row__artifacts-status">loading…</span>
    {:else if artifactsError}
      <span class="run-row__artifacts-status">{artifactsError}</span>
    {:else if artifacts}
      {#if artifacts.length === 0}
        <span class="run-row__artifacts-status">none</span>
      {:else}
        <ul class="run-row__artifacts-list">
          {#each artifacts as artifact (artifact.path)}
            <li>{artifact.path} ({artifact.bytes}b)</li>
          {/each}
        </ul>
      {/if}
    {/if}
  </td>
  <td class="run-row__actions">
    {#if run.status === 'awaiting_review'}
      <button type="button" onclick={onAccept}>Accept</button>
      <button type="button" onclick={onReject}>Reject</button>
    {/if}
    {#if !isTerminal}
      <button type="button" onclick={onCancel}>Cancel</button>
    {/if}
    {#if run.status === 'parked'}
      <button type="button" onclick={onRequeue}>Requeue</button>
    {/if}
  </td>
</tr>
