<script lang="ts">
  import Icon from './Icon.svelte';
  import { api } from './api';

  type Capability = {
    project: string;
    configured: boolean;
    schema_validated: boolean;
    provider: string;
    model: string | null;
    image_digest: string | null;
    timeout_seconds: number | null;
    memory_bytes: number | null;
    nano_cpus: number | null;
    registries: string[];
    extra_hosts: string[];
    egress_hosts: string[];
    mode: string | null;
  };
  type Payload = {
    capabilities: Record<string, boolean>;
    readiness: Record<string, string>;
    dispatch: Capability[];
  };
  let { demo = false }: { demo?: boolean } = $props();
  let data = $state<Payload | null>(null);
  let loading = $state(false);
  let error = $state('');
  let controller: AbortController | null = null;
  const capabilityLabels: Record<string, string> = {
    github: 'GitHub',
    dispatch: 'Dispatch configuration',
    provider_credential: 'Provider credentials',
    quota: 'Quota ledger',
    docker: 'Docker',
    egress: 'Network egress',
  };

  function memory(value: number | null) {
    return value == null ? 'Not configured' : `${Math.round(value / 1024 / 1024)} MiB`;
  }
  function readinessLabel(name: string, configured: boolean) {
    const value = data?.readiness[name];
    if (value === 'validated') return 'Schema validated';
    if (value === 'missing') return 'File missing';
    if (value === 'invalid') return 'Invalid configuration';
    return configured || value === 'configured' ? 'Configured' : 'Not configured';
  }
  async function load() {
    if (demo) return;
    controller?.abort();
    const requestController = new AbortController();
    controller = requestController;
    loading = true;
    error = '';
    try {
      const result = await api<Payload>('/capabilities', {
        signal: AbortSignal.any([requestController.signal, AbortSignal.timeout(15_000)]),
      });
      if (!requestController.signal.aborted && controller === requestController) data = result;
    } catch (reason) {
      if (!requestController.signal.aborted)
        error =
          reason instanceof Error
            ? reason.message
            : 'Capabilities are unavailable. Retry to check the manager.';
    } finally {
      if (!requestController.signal.aborted && controller === requestController) loading = false;
    }
  }
  $effect(() => {
    if (demo) {
      controller?.abort();
      loading = false;
      return;
    }
    void load();
    return () => controller?.abort();
  });
</script>

<section class="capabilities" aria-labelledby="capabilities-title">
  <div class="capability-heading">
    <h2 id="capabilities-title">Backend capabilities</h2>
    {#if demo}<span class="sample">SAMPLE ONLY</span>{:else}<button
        class="refresh"
        onclick={load}
        disabled={loading}
        aria-label="Refresh capabilities"><Icon name="refresh" size={16} /></button
      >{/if}
  </div>
  <p class="intro">
    Configuration and schema validation are reported separately. These checks do not verify live
    connectivity.
  </p>
  {#if loading}<p class="muted">Checking configured capabilities…</p>
  {:else if error}<div class="error" role="alert">
      <span>{error}</span><button class="retry" onclick={load}>Retry</button>
    </div>
  {:else if demo}<p class="muted">Connect a manager to inspect its configured integrations.</p>
  {:else if data}<div class="capability-list">
      {#each Object.entries(data.capabilities) as [name, configured] (name)}
        <div class="capability-row">
          <span>{capabilityLabels[name] ?? name}</span><strong class:ready={configured}
            >{readinessLabel(name, configured)}</strong
          >
        </div>
      {/each}
    </div>
    {#each data?.dispatch ?? [] as item (item.project)}<details class="dispatch">
        <summary
          ><span>{item.project}</span><small
            >{item.schema_validated
              ? 'Schema validated'
              : item.configured
                ? 'Configured, schema not validated'
                : 'Not configured'}
            <Icon name="chevron" size={15} /></small
          ></summary
        >
        <dl>
          <div>
            <dt>Mode</dt>
            <dd>{item.mode ?? 'Not configured'}</dd>
          </div>
          <div>
            <dt>Provider / model</dt>
            <dd>{item.provider} / {item.model ?? 'Not configured'}</dd>
          </div>
          <div>
            <dt>Image digest</dt>
            <dd><code>{item.image_digest ?? 'Not configured'}</code></dd>
          </div>
          <div>
            <dt>Timeout / memory</dt>
            <dd>
              {item.timeout_seconds ? `${item.timeout_seconds}s` : 'Not configured'} / {memory(
                item.memory_bytes,
              )}
            </dd>
          </div>
          <div>
            <dt>CPU limit</dt>
            <dd>
              {item.nano_cpus
                ? `${(item.nano_cpus / 1_000_000_000).toFixed(2)} vCPU`
                : 'Not configured'}
            </dd>
          </div>
          <div>
            <dt>Safe egress hosts</dt>
            <dd>{item.egress_hosts.length ? item.egress_hosts.join(', ') : 'None configured'}</dd>
          </div>
          <div>
            <dt>Registries / extra hosts</dt>
            <dd>
              {item.registries.length || item.extra_hosts.length
                ? `${item.registries.join(', ') || 'none'} / ${item.extra_hosts.join(', ') || 'none'}`
                : 'None configured'}
            </dd>
          </div>
        </dl>
      </details>{/each}
  {/if}
</section>

<style>
  .capabilities {
    background: #fff;
    border: 1px solid #dce5f0;
    border-radius: 12px;
    color: #172b4d;
    padding: 20px;
    font:
      14px/1.5 'Geist Variable',
      sans-serif;
  }
  .capability-heading,
  .capability-row,
  summary {
    align-items: center;
    display: flex;
  }
  .capability-heading {
    justify-content: space-between;
    gap: 12px;
  }
  h2 {
    font-size: 18px;
    margin: 0;
  }
  .intro,
  .muted {
    color: #52657e;
    margin: 8px 0 16px;
  }
  .sample {
    color: #97601b;
    font-size: 12px;
    font-weight: 650;
    letter-spacing: 0.04em;
  }
  .refresh {
    background: #fff;
    border: 1px solid #b9cbe3;
    border-radius: 7px;
    color: #344d70;
    cursor: pointer;
    min-height: 44px;
    min-width: 44px;
  }
  .refresh:disabled {
    cursor: wait;
    opacity: 0.5;
  }
  .capability-list {
    border-top: 1px solid #e6edf5;
  }
  .capability-row {
    border-bottom: 1px solid #edf1f6;
    gap: 12px;
    min-height: 44px;
  }
  .capability-row span {
    flex: 1;
  }
  .capability-row strong {
    color: #8d3c2e;
    font-size: 13px;
  }
  .capability-row strong.ready {
    color: #2463eb;
  }
  .dispatch {
    border-bottom: 1px solid #e6edf5;
  }
  summary {
    cursor: pointer;
    justify-content: space-between;
    list-style: none;
    min-height: 48px;
  }
  summary::-webkit-details-marker {
    display: none;
  }
  summary span {
    font-weight: 600;
  }
  summary small {
    align-items: center;
    color: #52657e;
    display: inline-flex;
    gap: 6px;
  }
  summary :global(svg) {
    transition: transform 180ms ease;
  }
  details[open] summary :global(svg) {
    transform: rotate(90deg);
  }
  dl {
    display: grid;
    gap: 12px 20px;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    margin: 0 0 16px;
  }
  dt {
    color: #63758c;
    font-size: 13px;
  }
  dd {
    margin: 2px 0 0;
    overflow-wrap: anywhere;
  }
  code {
    font:
      12px 'Cascadia Code',
      monospace;
  }
  .error {
    color: #8d3c2e;
  }
  button:focus-visible,
  summary:focus-visible {
    outline: 2px solid #2463eb;
    outline-offset: 3px;
  }
  @media (max-width: 560px) {
    dl {
      grid-template-columns: 1fr;
    }
    .capability-row {
      align-items: flex-start;
      flex-wrap: wrap;
      padding: 8px 0;
    }
  }
</style>
