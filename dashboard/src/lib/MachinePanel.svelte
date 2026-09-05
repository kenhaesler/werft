<script lang="ts">
  import Icon from './Icon.svelte';
  import { bytes } from './format';
  import type { Machine, RunSummary } from './types';
  let {
    machine,
    error = '',
    compact = false,
    demo = false,
    runs = [],
    onmanage,
    onselect,
    oninspect,
    onrefresh,
  }: {
    machine: Machine | null;
    error?: string;
    compact?: boolean;
    demo?: boolean;
    runs?: RunSummary[];
    onmanage?: () => void;
    onselect?: (run: RunSummary) => void;
    oninspect?: (id: string) => Promise<void>;
    onrefresh?: () => void;
  } = $props();
  let inspectingId = $state<string | null>(null);
  let inspectError = $state('');

  async function inspectContainer(container: Machine['containers'][number], run?: RunSummary) {
    inspectError = '';
    if (oninspect) {
      inspectingId = container.run_id;
      try {
        await oninspect(container.run_id);
      } catch {
        inspectError = 'Task details could not be loaded. Retry the inspection.';
      } finally {
        inspectingId = null;
      }
    } else if (run) onselect?.(run);
  }
</script>

<section class:compact class="machine-panel">
  <div class="section-heading">
    <h2>{compact ? 'Your machine' : 'Host environment'}</h2>
    <span class="tiny-label">{demo ? 'SAMPLE' : 'DOCKER HOST'}</span>
  </div>
  {#if machine}
    <div class="machine-visual" aria-hidden="true">
      <div class="server-unit">
        <div class="server-top"><span class="server-mark">W</span><span>WERFT ENGINE</span></div>
        <div class="server-front">
          <span class="server-slits"></span><span class="server-light"></span><span
            class="server-light second"
          ></span><span class="server-port"></span>
        </div>
      </div>
      <span class="machine-orbit orbit-one"></span><span class="machine-orbit orbit-two"></span>
    </div>
    <div class="machine-identity">
      <div>
        <h3>{machine.name}</h3>
        <p>{machine.os} <span>·</span> {machine.architecture}</p>
      </div>
      <span class="status status-running"><i></i>Online</span>
    </div>
    <div class="machine-specs">
      <div>
        <Icon name="cpu" size={16} /><span>Compute</span><strong>{machine.cpus} vCPU</strong>
      </div>
      <div>
        <Icon name="memory" size={16} /><span>Memory</span><strong
          >{bytes(machine.memory_bytes)}</strong
        >
      </div>
      <div>
        <Icon name="vm" size={16} /><span>Engine</span><strong
          >Docker {machine.engine_version}</strong
        >
      </div>
    </div>
    <div class="capacity-heading">
      <span>Running containers</span><strong
        >{machine.containers.filter((c) => c.state === 'running').length}<span>
          / {machine.max_concurrent_runs} slots</span
        ></strong
      >
    </div>
    <p class="capacity-note">Scheduler admission also considers the manager driver.</p>
    <div
      class="capacity-track"
      aria-label={`${machine.containers.filter((c) => c.state === 'running').length} running containers; ${machine.max_concurrent_runs} concurrent run slots`}
    >
      <span
        style:width={`${Math.min(100, (machine.containers.filter((c) => c.state === 'running').length / Math.max(1, machine.max_concurrent_runs)) * 100)}%`}
      ></span>
    </div>
    {#if compact}<button class="button full subtle" onclick={onmanage}
        >Manage machine<Icon name="arrow" size={16} /></button
      >{:else}
      <div class="section-heading workload-heading">
        <h3>Agent environments</h3>
        <button
          class="icon-button"
          title="Refresh machine"
          aria-label="Refresh machine"
          onclick={onrefresh}><Icon name="refresh" size={16} /></button
        >
      </div>
      {#each machine.containers as container (container.id)}
        {@const run = runs.find((item) => item.id === container.run_id)}
        <div class="container-row">
          <Icon name="terminal" />
          <div>
            <strong>{container.name}</strong><small
              >{container.id.slice(0, 12)} <span>·</span> {container.status}</small
            >
          </div>
          {#if oninspect || run}<button
              class="button small"
              disabled={inspectingId === container.run_id}
              onclick={() => inspectContainer(container, run)}
              >{inspectingId === container.run_id ? 'Loading…' : 'Inspect'}</button
            >{:else}<span class="status">{container.state}</span>{/if}
        </div>
        <details class="container-details">
          <summary>Technical details</summary>
          <dl>
            <div>
              <dt>Image</dt>
              <dd><code>{container.image}</code></dd>
            </div>
            <div>
              <dt>Container ID</dt>
              <dd><code>{container.id}</code></dd>
            </div>
          </dl>
        </details>
      {:else}<p class="muted empty-inline">
          No agent environments are running. They are created when approved work is dispatched.
        </p>{/each}
      <div class="machine-note">
        <Icon name="shield" />
        <p>
          Werft manages the VM’s agent environments. Inspect a workload to view evidence or cancel
          its run. Host power, snapshots, and interactive shell access are not exposed by this
          manager.
        </p>
      </div>
      {#if inspectError}<p class="machine-error" role="alert">{inspectError}</p>{/if}
    {/if}
  {:else}<div class="empty-state machine-empty">
      <Icon name="vm" size={34} />
      <h3>Machine unavailable</h3>
      <p>{error || 'Connect your manager to see its Docker host and agent environments.'}</p>
      {#if onrefresh}<button class="button" onclick={onrefresh}>Retry connection</button>{/if}
    </div>{/if}
</section>

<style>
  .capacity-note,
  .machine-error,
  .container-details {
    font-size: 14px;
    line-height: 1.5;
  }
  .capacity-note {
    color: var(--muted);
    margin: 6px 0 12px;
  }
  .machine-error {
    color: #9a481d;
    margin: 14px 0 0;
  }
  .container-details {
    margin: -8px 0 14px 32px;
    color: var(--muted);
  }
  .container-details summary {
    cursor: pointer;
  }
  .container-details dl {
    display: grid;
    gap: 8px;
    margin: 10px 0 0;
  }
  .container-details dt {
    color: var(--muted);
    font-size: 12px;
  }
  .container-details dd {
    margin: 2px 0 0;
    overflow-wrap: anywhere;
  }
  .container-details code {
    font-size: 12px;
  }
</style>
