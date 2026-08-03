<script lang="ts">
  import type { RunSummary } from './types';

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
    <a href={`/api/v1/runs/${run.id}/artifacts`}>Artifacts</a>
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
