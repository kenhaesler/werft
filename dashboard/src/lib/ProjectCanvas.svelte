<script lang="ts">
  import { tick } from 'svelte';
  import Icon from './Icon.svelte';
  import WerftOrb from './WerftOrb.svelte';
  import { statusLabels } from './format';
  import type { ActivitySnapshot, Project, RunSummary, RunStatus } from './types';

  let {
    projects,
    runs,
    activity,
    demo,
    error,
    loading,
    project,
    selectedRunId = null,
    onproject,
    onrun,
    onnewtask,
    onnewproject,
    onrefresh,
    ontalk = () => {},
  }: {
    projects: Project[];
    runs: RunSummary[];
    activity: ActivitySnapshot | null;
    demo: boolean;
    error: string;
    loading: boolean;
    project: Project | null;
    selectedRunId?: string | null;
    onproject: (project: Project | null) => void;
    onrun: (run: RunSummary) => void;
    onnewtask: () => void;
    onnewproject: () => void;
    onrefresh: () => void;
    ontalk?: () => void;
  } = $props();
  const attentionStatuses: RunStatus[] = ['awaiting_review', 'blocked_quota', 'failed', 'parked'];
  const activeStatuses: RunStatus[] = ['claimed', 'running', 'awaiting_ci', 'merging'];
  const pipeline = ['Task', 'Agent', 'Checks', 'Result'];
  let workbench = $state<HTMLDivElement>();
  let fleet = $state<HTMLDivElement>();
  let fleetLinks = $state<string[]>([]);
  let links = $state<string[]>([]);
  let orbPaused = $state(false);
  $effect(() => {
    const element = fleet;
    void projects.length;
    if (!element || typeof ResizeObserver === 'undefined') return;
    const measure = () => {
      const frame = element.getBoundingClientRect();
      const hub = element.querySelector('.fleet-hub')?.getBoundingClientRect();
      if (!hub) return;
      const x = hub.left + hub.width / 2 - frame.left;
      const y = hub.bottom - frame.top;
      fleetLinks = [...element.querySelectorAll('.project-tile')].map((node) => {
        const box = node.getBoundingClientRect();
        const endX = box.left + box.width / 2 - frame.left;
        const endY = box.top - frame.top;
        return `M${x} ${y} C${x} ${y + 28} ${endX} ${endY - 28} ${endX} ${endY}`;
      });
    };
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    void tick().then(measure);
    return () => observer.disconnect();
  });
  $effect(() => {
    const element = workbench;
    void canvasRuns.length;
    void selectedRunId;
    if (!element || typeof ResizeObserver === 'undefined') return;
    const measure = () => {
      const frame = element.getBoundingClientRect();
      const hub = element.querySelector('.project-hub')?.getBoundingClientRect();
      if (!hub) return;
      const x = hub.left + hub.width / 2 - frame.left;
      const y = hub.bottom - frame.top;
      links = [...element.querySelectorAll('.workstream')].map((node) => {
        const box = node.getBoundingClientRect();
        const endX = box.left + box.width / 2 - frame.left;
        const endY = box.top - frame.top;
        return `M${x} ${y} C${x} ${y + 30} ${endX} ${endY - 30} ${endX} ${endY}`;
      });
    };
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    void tick().then(measure);
    return () => observer.disconnect();
  });
  const visibleRuns = $derived(runs.filter((run) => !['merged', 'canceled'].includes(run.status)));
  const canvasRuns = $derived(
    project ? visibleRuns.filter((run) => run.project_slug === project.slug) : [],
  );
  const activeProjectRuns = (item: Project) =>
    visibleRuns.filter(
      (run) => run.project_slug === item.slug && activeStatuses.includes(run.status),
    );
  const attentionCount = (item: Project) =>
    visibleRuns.filter(
      (run) => run.project_slug === item.slug && attentionStatuses.includes(run.status),
    ).length;
  const waitingCount = (item: Project) =>
    visibleRuns.filter((run) => run.project_slug === item.slug && run.status === 'queued').length;
  const runtimeFor = (run: RunSummary) =>
    activity?.active_runs.find((item) => item.run_id === run.id);
  const providerLabel = (run: RunSummary) =>
    run.status === 'queued' ? 'Unassigned' : (runtimeFor(run)?.provider ?? 'Provider not reported');
  const repoName = (item: Project) => item.repo.replace(/^.*\//, '');
  const stageFor = (status: RunStatus) => {
    if (['queued', 'claimed'].includes(status)) return 0;
    if (status === 'running') return 1;
    if (status === 'awaiting_ci') return 2;
    if (['awaiting_review', 'merging'].includes(status)) return 3;
    return -1;
  };
  const resultFor = (run: RunSummary) => {
    if (['running', 'claimed'].includes(run.status)) return 'Pending result';
    if (run.status === 'awaiting_review') return 'Ready for review';
    if (run.status === 'merging') return 'Merging';
    if (run.status === 'awaiting_ci') return 'Checks in progress';
    if (run.status === 'queued') return 'Awaiting assignment';
    return statusLabels[run.status];
  };
  function openNode(event: MouseEvent, run: RunSummary) {
    (event.currentTarget as HTMLElement).scrollIntoView({ block: 'nearest' });
    onrun(run);
  }
</script>

<section class="project-canvas" aria-label="Project workspace">
  <header class="canvas-header">
    <nav class="breadcrumbs" aria-label="Project navigation">
      <button class:current={!project} onclick={() => onproject(null)}>All projects</button>
      {#if project}<Icon name="chevron" size={14} /><span>{project.slug}</span>{/if}
    </nav>
    <div class="canvas-actions">
      <button
        class="icon-action"
        aria-label="Refresh projects"
        disabled={loading}
        onclick={onrefresh}><Icon name="refresh" size={17} /></button
      >
      {#if project}<button class="button primary" onclick={onnewtask}
          ><Icon name="plus" size={16} />New task</button
        >
      {:else}<button class="button primary" onclick={onnewproject}
          ><Icon name="plus" size={16} />New project</button
        >{/if}
    </div>
  </header>
  {#if error}<div class="canvas-error" role="alert">
      <Icon name="warning" size={18} />{error}
    </div>{/if}

  <div class="canvas-viewport" aria-busy={loading}>
    {#if loading && !runs.length}
      <div class="canvas-loading"><span class="spinner"></span>Loading projects…</div>
    {:else if project}
      <div class="workbench" bind:this={workbench}>
        <svg class="link-map" aria-hidden="true"
          >{#each links as path, index (index)}<path d={path} />{/each}</svg
        >
        <aside class="project-keel project-hub">
          <span class="keel-mark"><Icon name="projects" size={25} /></span>
          <div>
            <strong>{project.slug}</strong><span>{project.owner} / {repoName(project)}</span>
          </div>
          <div class="keel-count"><b>{canvasRuns.length}</b><span>open tasks</span></div>
        </aside>
        <div class="flow-legend" aria-label="Session flow stages">
          {#each pipeline as step, index (step)}<span><b>{index + 1}</b>{step}</span>{/each}
        </div>
        <div class="agent-stations">
          {#each canvasRuns as run (run.id)}
            {@const currentStage = stageFor(run.status)}
            <button
              class:selected={selectedRunId === run.id}
              class:in-flight={run.status === 'running'}
              class="workstream status-{run.status}"
              data-run-id={run.id}
              aria-label={`Open task ${run.issue_title}`}
              onclick={(event) => openNode(event, run)}
            >
              <div class="task-endcap">
                <span class="object-icon"><Icon name="projects" size={17} /></span><span
                  class="task-copy"
                  ><strong>{run.issue_title}</strong><small>#{run.issue_number}</small></span
                >
              </div>
              {#key run.status}<div
                  class="node-route"
                  aria-label={`Task progress: ${statusLabels[run.status]}`}
                >
                  {#each pipeline as stage, index (stage)}<span
                      class:reached={index <= currentStage}
                      class:now={index === currentStage}>{stage}</span
                    >{/each}
                </div>{/key}
              <div
                class:complete={currentStage >= 1}
                class:current={currentStage === 1}
                class="pipeline-piece agent-piece"
              >
                <span class="pipeline-icon"><Icon name="agent" size={16} /></span><span
                  ><b>{providerLabel(run)}</b><small>Agent</small></span
                >{#if run.status === 'running'}<i class="signal" aria-label="Running"></i>{/if}
              </div>
              <div
                class:complete={currentStage >= 2}
                class:current={currentStage === 2}
                class="pipeline-piece check-piece"
              >
                <span class="pipeline-icon"><Icon name="check" size={16} /></span><span
                  ><b>{run.status === 'awaiting_ci' ? 'Checking' : 'Checks'}</b><small
                    >Validation</small
                  ></span
                >
              </div>
              <div
                class:complete={currentStage >= 3}
                class:current={currentStage === 3}
                class="pipeline-piece result-piece"
              >
                <span class="status-dot"></span><span
                  ><b>{resultFor(run)}</b><small>{statusLabels[run.status]}</small></span
                >
              </div>
            </button>
          {:else}
            <div class="empty-canvas">
              <span class="empty-orbit"><Icon name="agent" size={32} /></span>
              <h2>No active sessions</h2>
              <p>Start an approved task to place an agent on this workbench.</p>
              <button class="button primary" onclick={onnewtask}
                ><Icon name="plus" size={16} />Create task</button
              >
            </div>
          {/each}
        </div>
      </div>
    {:else}
      <div class="fleet-floor" bind:this={fleet}>
        <div class="fleet-heading">
          <span>Projects</span>
          <p>{visibleRuns.length} open tasks across your workspace</p>
        </div>
        {#if projects.length}
          <svg class="link-map" aria-hidden="true"
            >{#each fleetLinks as path, index (index)}<path d={path} />{/each}</svg
          >
          <a
            class="fleet-hub"
            href="#talk"
            onclick={(event) => {
              event.preventDefault();
              ontalk();
            }}
            ><WerftOrb size={72} paused={orbPaused} /><span
              ><strong>Werft</strong><small>Orchestrator</small></span
            ><span class="hub-action">Ask or steer work<Icon name="arrow" size={15} /></span></a
          >
          <button
            class="fleet-motion"
            aria-label={orbPaused ? 'Play Werft animation' : 'Pause Werft animation'}
            title={orbPaused ? 'Play Werft animation' : 'Pause Werft animation'}
            onclick={() => (orbPaused = !orbPaused)}
            ><Icon name={orbPaused ? 'play' : 'pause'} size={14} /></button
          >
        {/if}
        <div class="portfolio-map">
          {#each projects as item, index (item.id)}
            {@const active = activeProjectRuns(item)}{@const attention =
              attentionCount(item)}{@const waiting = waitingCount(item)}
            <button
              class:needs-attention={attention > 0}
              class="project-tile berth-{index % 4}"
              onclick={() => onproject(item)}
            >
              <span class="tile-handle"></span><span class="tile-symbol"
                ><Icon name="projects" size={22} /></span
              ><span class="tile-body"
                ><small>{item.owner}</small><strong>{item.slug}</strong><span>{repoName(item)}</span
                ></span
              ><Icon name="arrow" size={17} />
              <span class="tile-stats"
                ><span><b>{active.length}</b> active</span>{#if attention}<em
                    ><Icon name="warning" size={13} />{attention} needs attention</em
                  >{:else if waiting}<span class="waiting-count"
                    ><Icon name="clock" size={13} />{waiting} waiting</span
                  >{/if}</span
              >
            </button>
          {:else}
            <div class="empty-canvas">
              <span class="empty-orbit"><Icon name="projects" size={32} /></span>
              <h2>No projects yet</h2>
              <p>Create a project to give approved work a home.</p>
              <button class="button primary" onclick={onnewproject}
                ><Icon name="plus" size={16} />New project</button
              >
            </div>
          {/each}
        </div>
      </div>
    {/if}
  </div>
  {#if demo}<p class="preview-note">Preview data · task activity is illustrative</p>{/if}
</section>

<style>
  .fleet-motion {
    position: absolute;
    right: 20px;
    top: 98px;
    width: 30px;
    height: 30px;
    display: grid;
    place-items: center;
    border: 1px solid var(--border);
    border-radius: 50%;
    background: var(--panel);
    color: var(--muted);
    z-index: 2;
  }
  .project-canvas {
    --floor: #eef3fa;
    --ink: var(--text);
    --line: #9bafca;
    --metal: #d0ddeb;
    --blue: var(--accent);
    color: var(--ink);
    container-type: inline-size;
  }
  :global([data-theme='dark']) .project-canvas {
    --floor: #111a28;
    --line: #547092;
    --metal: #344b68;
  }
  :global([data-theme='dark']) .workstream.selected {
    --panel: #c4cdff;
    --text: #151b35;
    --muted: #394462;
    --accent: #39499b;
    --panel-hover: #dce2ff;
    --border: #9eaad7;
    --line: #8796c6;
  }
  .canvas-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 0 0 16px;
    min-height: 58px;
  }
  .breadcrumbs,
  .canvas-actions {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .breadcrumbs button {
    background: none;
    border: 0;
    color: var(--muted);
    padding: 10px 8px;
    font-size: 14px;
  }
  .breadcrumbs span {
    font-weight: 650;
    font-size: 14px;
  }
  .breadcrumbs button:hover {
    color: var(--accent);
  }
  .icon-action {
    display: grid;
    place-items: center;
    width: 42px;
    height: 42px;
    border: 1px solid var(--border);
    background: var(--panel);
    color: var(--text);
    border-radius: 10px;
  }
  .canvas-viewport {
    position: relative;
    background: var(--floor);
    border: 1px solid var(--border);
    border-radius: 22px;
    overflow: auto;
    max-height: calc(100dvh - 225px);
    min-height: 440px;
  }
  .fleet-floor {
    padding: clamp(24px, 4cqw, 56px);
  }
  .fleet-heading {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 20px;
    margin-bottom: 44px;
  }
  .fleet-heading > span {
    font-size: 34px;
    font-weight: 650;
    letter-spacing: -0.035em;
  }
  .fleet-heading p {
    font-size: 14px;
    color: var(--muted);
  }
  .portfolio-map {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(280px, 100%), 1fr));
    gap: 32px;
    perspective: 1200px;
    padding-bottom: 25px;
  }
  .project-tile {
    position: relative;
    min-height: 260px;
    display: grid;
    grid-template-columns: 1fr auto;
    align-content: end;
    gap: 16px;
    padding: 26px;
    text-align: left;
    color: var(--text);
    background: linear-gradient(145deg, var(--panel), var(--floor));
    border: 1px solid var(--line);
    border-radius: 18px;
    box-shadow:
      0 10px 24px -12px var(--metal),
      0 24px 30px -20px #0c244844;
    transition:
      transform 0.24s ease,
      box-shadow 0.24s ease;
  }
  .project-tile:hover {
    transform: translateY(-7px);
    box-shadow:
      0 16px 28px -12px var(--metal),
      0 28px 36px -20px #0c244866;
  }
  .tile-symbol {
    position: absolute;
    top: 30px;
    left: 30px;
    width: 66px;
    height: 56px;
    display: grid;
    place-items: center;
    background: linear-gradient(
      140deg,
      color-mix(in srgb, var(--accent) 18%, var(--panel)),
      var(--panel)
    );
    color: var(--accent);
    border: 1px solid var(--line);
    border-radius: 12px;
    transform: perspective(320px) rotateX(22deg) rotateY(-18deg);
    box-shadow: 3px 6px 0 var(--metal);
  }
  .tile-symbol::before {
    content: '';
    position: absolute;
    inset: 9px;
    border: 1px solid var(--line);
    border-radius: 6px;
  }
  .tile-handle {
    position: absolute;
    top: 27px;
    right: 26px;
    width: 40px;
    border-top: 3px solid var(--line);
    border-bottom: 3px solid var(--line);
    height: 9px;
    opacity: 0.65;
  }
  .tile-body {
    display: grid;
    gap: 4px;
    min-width: 0;
  }
  .tile-body small {
    color: var(--muted);
    font-size: 12px;
  }
  .tile-body strong {
    font-size: 22px;
    letter-spacing: -0.025em;
    overflow-wrap: anywhere;
  }
  .tile-body > span {
    display: none;
  }
  .tile-stats {
    grid-column: 1/-1;
    display: flex;
    justify-content: space-between;
    gap: 8px;
    padding-top: 15px;
    border-top: 1px solid var(--border);
    font-size: 13px;
    color: var(--muted);
  }
  .tile-stats b {
    color: var(--text);
  }
  .tile-stats em,
  .waiting-count {
    font-style: normal;
    color: var(--amber);
    display: flex;
    align-items: center;
    gap: 5px;
  }
  .workbench {
    position: relative;
    padding: 38px 28px 32px;
    min-height: 480px;
  }
  .project-keel {
    position: relative;
    z-index: 1;
    width: min(420px, 100%);
    margin: 0 auto 76px;
    display: flex;
    align-items: center;
    gap: 18px;
    padding: 24px;
    background: linear-gradient(135deg, var(--panel), var(--floor));
    border: 1px solid var(--line);
    border-radius: 18px;
    box-shadow: 0 14px 26px -16px var(--metal);
  }
  .project-keel > div {
    display: grid;
    gap: 5px;
    min-width: 0;
  }
  .project-keel strong {
    font-size: 23px;
    letter-spacing: -0.03em;
    overflow-wrap: anywhere;
  }
  .project-keel span {
    font-size: 13px;
    color: var(--muted);
  }
  .keel-mark {
    display: grid;
    place-items: center;
    width: 52px;
    height: 52px;
    flex-shrink: 0;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: var(--panel-hover);
    color: var(--accent) !important;
    box-shadow: 0 4px 0 var(--metal);
  }
  .keel-count {
    margin-left: auto;
    border-left: 1px solid var(--border);
    padding-left: 16px;
    white-space: nowrap;
  }
  .keel-count b {
    font-size: 27px;
    color: var(--accent);
  }
  .flow-legend {
    display: none;
  }
  .link-map {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    overflow: visible;
    pointer-events: none;
  }
  .link-map path {
    fill: none;
    stroke: var(--line);
    stroke-width: 1.6;
  }
  .agent-stations {
    position: relative;
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 44px 26px;
  }
  .workstream {
    position: relative;
    z-index: 1;
    flex: 0 1 300px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 0;
    padding: 20px;
    text-align: left;
    color: var(--text);
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 16px;
    box-shadow: 0 12px 24px -16px var(--metal);
    transition:
      border-color 0.2s,
      box-shadow 0.2s;
  }
  .workstream:hover,
  .workstream.selected {
    border-color: var(--accent);
    box-shadow:
      0 12px 24px -16px var(--metal),
      0 0 0 3px color-mix(in srgb, var(--accent) 24%, transparent);
  }
  .task-endcap {
    order: 2;
    display: flex;
    gap: 8px;
    align-items: start;
    padding: 16px 0 18px;
  }
  .object-icon {
    display: none;
  }
  .task-copy {
    display: grid;
    gap: 7px;
  }
  .task-copy strong {
    font-size: 16px;
    line-height: 1.4;
    letter-spacing: -0.01em;
  }
  .task-copy small {
    font-size: 12px;
    color: var(--muted);
  }
  .agent-piece {
    order: 1;
    display: flex;
    gap: 12px;
    align-items: center;
    border-bottom: 1px solid var(--border);
    padding-bottom: 16px;
    position: relative;
  }
  .agent-piece > span:last-of-type {
    display: grid;
    gap: 3px;
  }
  .agent-piece b {
    font-size: 14px;
  }
  .agent-piece small {
    font-size: 12px;
    color: var(--muted);
  }
  .pipeline-icon {
    display: grid;
    place-items: center;
    width: 44px;
    height: 44px;
    border-radius: 14px;
    background: var(--panel-hover);
    color: var(--accent);
    border: 1px solid var(--line);
    box-shadow: 0 3px 0 var(--metal);
  }
  .check-piece {
    display: none;
  }
  .result-piece {
    order: 4;
    display: flex;
    gap: 8px;
    align-items: center;
    padding-top: 16px;
    font-size: 13px;
  }
  .result-piece span:last-child {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .result-piece small {
    color: var(--muted);
    font-size: 12px;
  }
  .status-dot {
    width: 7px;
    height: 7px;
    background: var(--accent);
    border-radius: 50%;
    flex-shrink: 0;
  }
  .status-failed .status-dot,
  .status-parked .status-dot,
  .status-blocked_quota .status-dot {
    background: var(--amber);
  }
  .signal {
    width: 6px;
    height: 6px;
    background: var(--accent);
    border-radius: 50%;
    margin-left: auto;
  }
  .node-route {
    order: 3;
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 4px;
  }
  .node-route span {
    padding-top: 8px;
    border-top: 3px solid var(--border);
    font-size: 11px;
    color: var(--muted);
    text-align: center;
  }
  .node-route .reached {
    border-color: color-mix(in srgb, var(--accent) 45%, var(--panel));
  }
  .node-route .now {
    border-color: var(--accent);
    color: var(--accent);
    font-weight: 650;
    animation: stage-arrival 650ms ease-out;
  }
  @keyframes stage-arrival {
    from {
      background: color-mix(in srgb, var(--accent) 20%, transparent);
    }
    to {
      background: transparent;
    }
  }
  .empty-canvas {
    display: grid;
    justify-items: center;
    gap: 14px;
    padding: 26px;
    grid-column: 1/-1;
    text-align: center;
  }
  .empty-canvas h2 {
    font-size: 22px;
    margin: 0;
  }
  .empty-canvas p {
    font-size: 14px;
    color: var(--muted);
    margin: 0;
  }
  .empty-orbit {
    color: var(--accent);
  }
  .preview-note {
    font-size: 12px;
    color: var(--muted);
    margin: 12px 4px 0;
  }
  .canvas-error {
    display: flex;
    gap: 12px;
    padding: 16px;
    background: var(--warning-soft);
    color: var(--amber);
    margin-bottom: 12px;
    border-radius: 12px;
  }
  .canvas-loading {
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 440px;
    gap: 12px;
    color: var(--muted);
  }
  @container (max-width:650px) {
    .project-tile {
      min-height: 200px;
      padding: 20px;
    }
    .tile-symbol {
      top: 18px;
      left: 20px;
      width: 48px;
      height: 40px;
    }
    .portfolio-map {
      gap: 24px;
    }
    .fleet-heading {
      display: block;
      margin-bottom: 28px;
    }
    .fleet-heading > span {
      font-size: 28px;
    }
    .canvas-viewport {
      max-height: none;
    }
    .workbench {
      padding: 26px 16px;
    }
    .project-keel {
      padding: 20px;
      gap: 12px;
    }
    .keel-count {
      display: none;
    }
    .project-keel strong {
      font-size: 20px;
    }
    .agent-stations {
      gap: 32px;
    }
    .workstream {
      flex-basis: 300px;
    }
    .canvas-header {
      gap: 6px;
    }
    .canvas-actions {
      gap: 6px;
    }
    .breadcrumbs {
      gap: 2px;
    }
    .breadcrumbs span {
      max-width: 110px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .node-route .now {
      animation: none;
    }
    .project-tile,
    .workstream {
      transition: none;
    }
    .project-tile:hover {
      transform: none;
    }
  }
  .fleet-floor {
    position: relative;
  }
  .fleet-heading {
    position: relative;
    z-index: 1;
    margin-bottom: 24px;
  }
  .fleet-hub {
    position: relative;
    z-index: 1;
    display: flex;
    align-items: center;
    gap: 12px;
    width: fit-content;
    max-width: 100%;
    margin: 0 auto 46px;
    padding: 14px 20px;
    border: 1px solid var(--line);
    border-radius: 16px;
    background: var(--panel);
  }
  .fleet-hub > span {
    display: grid;
    gap: 3px;
  }
  .fleet-hub strong {
    font-size: 17px;
  }
  .fleet-hub small {
    font-size: 12px;
    color: var(--muted);
  }
  .fleet-hub .hub-action {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-left: 30px;
    font-size: 13px;
    color: var(--accent);
  }
  .needs-attention {
    border-color: var(--amber);
    background: linear-gradient(
      145deg,
      var(--panel),
      color-mix(in srgb, var(--warning-soft) 50%, var(--panel))
    );
  }
  .needs-attention .tile-handle {
    border-color: var(--amber);
  }
  .needs-attention .tile-symbol {
    color: var(--amber);
    border-color: var(--amber);
  }
  @container (max-width:650px) {
    .fleet-hub {
      margin-bottom: 32px;
      padding: 12px 16px;
    }
    .fleet-hub .hub-action {
      margin-left: 8px;
      font-size: 12px;
    }
    .fleet-heading {
      margin-bottom: 20px;
    }
    .fleet-heading p {
      margin-bottom: 12px;
    }
    .project-tile {
      min-height: 178px;
    }
    .tile-symbol {
      width: 40px;
      height: 32px;
      top: 14px;
    }
    .tile-body strong {
      font-size: 20px;
    }
    .portfolio-map {
      gap: 20px;
    }
    .fleet-floor {
      padding: 20px;
    }
  }
</style>
