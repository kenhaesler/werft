<script lang="ts">
  import type { RunStatus } from './types';
  import { statusLabels } from './format';
  import Icon from './Icon.svelte';
  let { status }: { status: RunStatus } = $props();
  const steps = ['Preparing', 'Working', 'Checks', 'Review', 'Completed'];
  const icons = ['projects', 'agent', 'review', 'check', 'branch'];
  const positions: Record<RunStatus, number> = {
    queued: 0,
    claimed: 0,
    blocked_quota: 0,
    running: 1,
    failed: -1,
    parked: -1,
    awaiting_ci: 2,
    awaiting_review: 3,
    merging: 4,
    merged: 4,
    canceled: -1,
  };
  const current = $derived(positions[status]);
  const interrupted = $derived(['failed', 'parked', 'blocked_quota', 'canceled'].includes(status));
</script>

<section class="run-progress" aria-label="Task progress">
  <div class="progress-title">
    <strong>Task progress</strong><span class:interrupted>{statusLabels[status]}</span>
  </div>
  <ol>
    {#each steps as step, index (step)}
      <li
        class:current={index === current}
        class:passed={!interrupted && index < current}
        class:interrupted={interrupted && index === current}
        aria-current={index === current ? 'step' : undefined}
      >
        <span class="stage-dot" aria-hidden="true"><Icon name={icons[index]} size={17} /></span
        ><span>{step === 'Completed' && status === 'merging' ? 'Merging' : step}</span>
      </li>
    {/each}
  </ol>
  {#if interrupted}<p>Execution stopped. See the task status below.</p>{/if}
</section>

<style>
  .run-progress {
    padding: 20px 0 24px;
    margin-bottom: 18px;
    border-bottom: 1px solid var(--border);
  }
  .progress-title {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    font-size: 14px;
  }
  .progress-title span {
    color: var(--accent);
  }
  ol {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    padding: 0;
    margin: 20px 0 0;
    list-style: none;
  }
  li {
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 10px;
    color: var(--muted);
    font-size: 12px;
  }
  li:not(:last-child)::after {
    content: '';
    position: absolute;
    height: 1px;
    background: var(--border);
    top: 18px;
    left: calc(50% + 22px);
    width: calc(100% - 44px);
  }
  .stage-dot {
    display: grid;
    place-items: center;
    width: 36px;
    height: 36px;
    border-radius: 12px;
    border: 1px solid var(--border);
    background: var(--panel);
  }
  .current {
    color: var(--accent);
    font-weight: 650;
  }
  .current .stage-dot {
    border-color: var(--accent);
    color: var(--accent-ink);
    background: var(--accent);
    box-shadow: 0 0 0 5px color-mix(in srgb, var(--accent) 12%, transparent);
  }
  .passed .stage-dot {
    color: var(--accent);
    background: color-mix(in srgb, var(--accent) 9%, var(--panel));
  }
  .passed::after {
    background: var(--accent) !important;
  }
  .interrupted {
    color: var(--amber) !important;
  }
  .interrupted .stage-dot {
    border-color: var(--amber);
    background: var(--amber);
  }
  p {
    margin: 15px 0 0;
    color: var(--amber);
    font-size: 13px;
  }
</style>
