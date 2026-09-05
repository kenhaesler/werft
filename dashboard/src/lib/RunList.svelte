<script lang="ts">
  import Icon from './Icon.svelte';
  import { relativeTime, statusLabels } from './format';
  import type { RunSummary } from './types';
  let {
    runs,
    onselect,
    empty = 'No runs here yet.',
  }: { runs: RunSummary[]; onselect: (run: RunSummary) => void; empty?: string } = $props();
</script>

<div class="run-list">
  <div class="run-list-head">
    <span>Task</span><span>Status</span><span>Updated</span><span></span>
  </div>
  {#each runs as run (run.id)}
    <button class="run-item" onclick={() => onselect(run)}>
      <span class="run-task"
        ><span
          class="project-symbol {run.project_slug.includes('data')
            ? 'orange'
            : run.project_slug.includes('design')
              ? 'violet'
              : ''}"
          ><Icon
            name={run.project_slug.includes('data')
              ? 'activity'
              : run.project_slug.includes('design')
                ? 'overview'
                : 'projects'}
            size={18}
          /></span
        ><span
          ><strong>{run.issue_title}</strong><small
            >{run.project_slug}<span class="meta-dot">·</span>#{run.issue_number}</small
          ></span
        ></span
      >
      <span class="status status-{run.status}"><i></i>{statusLabels[run.status]}</span>
      <span class="run-time">{relativeTime(run.updated_at)}</span><Icon name="chevron" size={15} />
    </button>
  {:else}
    <div class="empty-state">
      <Icon name="agent" size={30} />
      <h3>{empty}</h3>
      <p>Approved work will appear here as your agents get started.</p>
    </div>
  {/each}
</div>
