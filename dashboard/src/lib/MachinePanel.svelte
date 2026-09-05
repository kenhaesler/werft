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
    onrefresh,
  }: {
    machine: Machine | null;
    error?: string;
    compact?: boolean;
    demo?: boolean;
    runs?: RunSummary[];
    onmanage?: () => void;
    onselect?: (run: RunSummary) => void;
    onrefresh?: () => void;
  } = $props();
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
      <span>Runner capacity</span><strong
        >{machine.containers.filter((c) => c.state === 'running').length}<span>
          / {machine.max_concurrent_runs} slots</span
        ></strong
      >
    </div>
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
          {#if run}<button class="button small" onclick={() => onselect?.(run)}>Inspect</button
            >{:else}<span class="status">{container.state}</span>{/if}
        </div>
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
    {/if}
  {:else}<div class="empty-state machine-empty">
      <Icon name="vm" size={34} />
      <h3>Machine unavailable</h3>
      <p>{error || 'Connect your manager to see its Docker host and agent environments.'}</p>
      {#if onrefresh}<button class="button" onclick={onrefresh}>Retry connection</button>{/if}
    </div>{/if}
</section>
