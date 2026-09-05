<script lang="ts">
  import type { RunStatus } from './types';
  import { statusLabels } from './format';
  let { status }: { status: RunStatus } = $props();
  const steps = ['Preparing', 'Working', 'Checks', 'Review', 'Completed'];
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
        class:interrupted={interrupted && index === current}
        aria-current={index === current ? 'step' : undefined}
      >
        <span class="stage-dot"></span><span
          >{step === 'Completed' && status === 'merging' ? 'Merging' : step}</span
        >
      </li>
    {/each}
  </ol>
  {#if interrupted}<p>Execution stopped. See the task status below.</p>{/if}
</section>

<style>
  .run-progress {
    padding: 18px 0 22px;
    margin-bottom: 18px;
    border-bottom: 1px solid #dce5f0;
  }
  .progress-title {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    font-size: 14px;
  }
  .progress-title span {
    color: #245edb;
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
    color: #52657e;
    font-size: 12px;
  }
  li:not(:last-child)::after {
    content: '';
    position: absolute;
    height: 1px;
    background: #dce5f0;
    top: 6px;
    left: calc(50% + 10px);
    width: calc(100% - 20px);
  }
  .stage-dot {
    width: 13px;
    height: 13px;
    border-radius: 50%;
    border: 1px solid #a7b7ce;
    background: #fff;
  }
  .current {
    color: #245edb;
    font-weight: 650;
  }
  .current .stage-dot {
    border-color: #245edb;
    background: #245edb;
  }
  .interrupted {
    color: #975315 !important;
  }
  .interrupted .stage-dot {
    border-color: #975315;
    background: #975315;
  }
  p {
    margin: 15px 0 0;
    color: #975315;
    font-size: 13px;
  }
</style>
