<script lang="ts">
  import Icon from './Icon.svelte';
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
  } = $props();

  const attentionStatuses: RunStatus[] = ['awaiting_review', 'blocked_quota', 'failed', 'parked'];
  const activeStatuses: RunStatus[] = ['claimed', 'running', 'awaiting_ci', 'merging'];
  const stages = ['Preparing', 'Working', 'Checks', 'Review', 'Done'];

  const visibleRuns = $derived(runs.filter((run) => !['merged', 'canceled'].includes(run.status)));
  const canvasRuns = $derived(
    project ? visibleRuns.filter((run) => run.project_slug === project.slug) : [],
  );
  const mapHeight = $derived(Math.max(560, 320 + Math.ceil(canvasRuns.length / 3) * 220));
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
  const providerFor = (run: RunSummary) => runtimeFor(run)?.provider ?? null;
  const stageFor = (status: RunStatus) => {
    if (status === 'queued') return 0;
    if (status === 'claimed') return 0;
    if (status === 'running') return 1;
    if (status === 'awaiting_ci') return 2;
    if (status === 'awaiting_review') return 3;
    if (['merging', 'merged'].includes(status)) return 4;
    return -1;
  };
  const providerLabel = (run: RunSummary) =>
    run.status === 'queued' ? 'Unassigned' : (providerFor(run) ?? 'Provider not reported');
  const repoName = (item: Project) => item.repo.replace(/^.*\//, '');
  function openNode(event: MouseEvent, run: RunSummary) {
    (event.currentTarget as HTMLElement).scrollIntoView({ behavior: 'smooth', block: 'nearest' });
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
        onclick={onrefresh}
      >
        <Icon name="refresh" size={17} />
      </button>
      {#if project}
        <button class="button primary" onclick={onnewtask}
          ><Icon name="plus" size={16} />New task</button
        >
      {:else}
        <button class="button primary" onclick={onnewproject}
          ><Icon name="plus" size={16} />New project</button
        >
      {/if}
    </div>
  </header>

  {#if error}<div class="canvas-error" role="alert">
      <Icon name="warning" size={18} />{error}
    </div>{/if}

  <div class="canvas-viewport" aria-busy={loading}>
    {#if loading && !runs.length}<div class="canvas-loading">
        <span class="spinner"></span>Loading projects…
      </div>
    {:else if project}<div class="project-map" style={`height: ${mapHeight}px`}>
        <svg class="connectors" viewBox={`0 0 1000 ${mapHeight}`} aria-hidden="true">
          {#each canvasRuns as run, index (run.id)}
            <path
              d={`M500 175 C500 225 ${195 + (index % 3) * 305} ${230 + Math.floor(index / 3) * 220} ${195 + (index % 3) * 305} ${280 + Math.floor(index / 3) * 220}`}
            />
          {/each}
        </svg>
        <div class="project-hub">
          <span class="hub-icon"><Icon name="projects" size={24} /></span>
          <div>
            <strong>{project.slug}</strong><span>{project.owner}/{repoName(project)}</span>
          </div>
        </div>
        {#each canvasRuns as run, index (run.id)}
          <button
            class:selected={selectedRunId === run.id}
            class="agent-node status-{run.status} node-{index % 3}"
            style={`left: ${70 + (index % 3) * 305}px; top: ${280 + Math.floor(index / 3) * 220}px`}
            data-run-id={run.id}
            aria-label={`Open task ${run.issue_title}`}
            onclick={(event) => openNode(event, run)}
          >
            <span class="node-top"
              ><span class="agent-mark"><Icon name="agent" size={18} /></span><span
                class="node-status"><i></i>{statusLabels[run.status]}</span
              ></span
            >
            <strong>{run.issue_title}</strong>
            <span class="node-meta">#{run.issue_number} · {providerLabel(run)}</span>
            {#if run.status === 'queued'}<span class="waiting"
                ><Icon name="clock" size={14} />Waiting for assignment</span
              >
            {:else}<span class="stage-track" aria-label={`${statusLabels[run.status]} progress`}>
                {#each stages as stage, stageIndex (stage)}<i
                    class:active={stageIndex === stageFor(run.status)}
                    title={stage}
                  ></i>{/each}
              </span>{/if}
          </button>
        {:else}<div class="empty-canvas">
            <span class="empty-orbit"><Icon name="agent" size={32} /></span>
            <h2>No active sessions</h2>
            <p>Agents start when an approved task is ready.</p>
            <button class="button primary" onclick={onnewtask}
              ><Icon name="plus" size={16} />Create task</button
            >
          </div>{/each}
      </div>
    {:else}<div class="portfolio-map">
        {#each projects as item, index (item.id)}
          {@const active = activeProjectRuns(item)}
          {@const attention = attentionCount(item)}
          {@const waiting = waitingCount(item)}
          <button class="project-tile tile-{index % 5}" onclick={() => onproject(item)}>
            <span class="tile-symbol"><Icon name="projects" size={22} /></span>
            <span class="tile-body"
              ><small>{item.owner}</small><strong>{item.slug}</strong><span>{repoName(item)}</span
              ></span
            >
            <span class="tile-stats"
              ><span><b>{active.length}</b> active work</span>{#if attention}<em
                  ><Icon name="warning" size={13} />{attention} needs attention</em
                >{:else if waiting}<span class="waiting-count"
                  ><Icon name="clock" size={13} />{waiting} waiting</span
                >{/if}</span
            >
            <Icon name="arrow" size={17} />
          </button>
        {:else}<div class="empty-canvas">
            <span class="empty-orbit"><Icon name="projects" size={32} /></span>
            <h2>No projects yet</h2>
            <p>Create a project to give approved work a home.</p>
            <button class="button primary" onclick={onnewproject}
              ><Icon name="plus" size={16} />New project</button
            >
          </div>{/each}
      </div>{/if}
  </div>
  {#if demo}<p class="preview-note">Preview data · task activity is illustrative</p>{/if}
</section>

<style>
  .project-canvas {
    color: #18314f;
    min-width: 0;
  }
  .canvas-header {
    min-height: 58px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 0 4px 12px;
  }
  .breadcrumbs,
  .canvas-actions,
  .node-top,
  .tile-stats,
  .waiting {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .breadcrumbs button {
    border: 0;
    background: none;
    color: #51708f;
    padding: 10px 8px;
    border-radius: 8px;
    font: 600 14px/1 inherit;
    cursor: pointer;
    min-height: 44px;
  }
  .breadcrumbs button.current,
  .breadcrumbs button:hover {
    color: #1255b5;
    background: #edf5ff;
  }
  .breadcrumbs span {
    color: #173557;
    font-size: 14px;
    font-weight: 700;
  }
  .canvas-actions {
    gap: 10px;
  }
  .button,
  .icon-action {
    min-height: 44px;
    border-radius: 9px;
    border: 1px solid #c9d9eb;
    background: white;
    color: #173557;
    cursor: pointer;
    font: 700 13px/1 inherit;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 7px;
    padding: 0 14px;
  }
  .button.primary {
    background: #1769d5;
    border-color: #1769d5;
    color: white;
    box-shadow: 0 5px 12px #1769d52b;
  }
  .icon-action {
    width: 44px;
    padding: 0;
  }
  .button:hover,
  .icon-action:hover {
    border-color: #6b9fdd;
    background: #f4f9ff;
  }
  .button.primary:hover {
    background: #105cbc;
  }
  button:focus-visible {
    outline: 3px solid #76b7ff;
    outline-offset: 3px;
  }
  button:disabled {
    cursor: wait;
    opacity: 0.55;
  }
  .canvas-error {
    margin: 0 0 12px;
    background: #fff5eb;
    border: 1px solid #f1c18e;
    color: #8a4d16;
    padding: 12px 14px;
    display: flex;
    gap: 9px;
    border-radius: 10px;
    font-size: 14px;
  }
  .canvas-viewport {
    height: 600px;
    max-height: calc(100vh - 220px);
    min-height: 420px;
    overflow: auto;
    position: relative;
    border: 1px solid #cbdff2;
    border-radius: 16px;
    background-color: #fbfdff;
    background-image: radial-gradient(#bbd1e9 1px, transparent 1.1px);
    background-size: 18px 18px;
    box-shadow:
      inset 0 0 0 1px #ffffff,
      0 10px 28px #0d407714;
  }
  .canvas-loading {
    height: 100%;
    display: grid;
    place-items: center;
    color: #54718f;
    font-weight: 600;
  }
  .spinner {
    width: 18px;
    height: 18px;
    border: 2px solid #c8dbef;
    border-top-color: #1769d5;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-right: 8px;
  }
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
  .portfolio-map {
    min-width: 760px;
    min-height: 570px;
    padding: 64px 56px;
    display: grid;
    grid-template-columns: repeat(3, minmax(200px, 1fr));
    align-content: center;
    gap: 44px 38px;
    position: relative;
  }
  .project-tile {
    min-height: 184px;
    text-align: left;
    display: grid;
    grid-template-columns: auto 1fr auto;
    align-items: start;
    gap: 12px;
    padding: 20px;
    border: 1px solid #b8d2ee;
    border-radius: 14px;
    background: #fff;
    color: #173557;
    cursor: pointer;
    box-shadow: 0 7px 16px #24517b12;
    transition:
      transform 0.18s,
      box-shadow 0.18s,
      border-color 0.18s;
    position: relative;
  }
  .project-tile:hover {
    transform: translateY(-3px);
    border-color: #4c8cdb;
    box-shadow: 0 12px 26px #24517b24;
  }
  .project-tile.tile-1 {
    transform: translateY(34px);
  }
  .project-tile.tile-3 {
    transform: translateY(-15px);
  }
  .project-tile.tile-4 {
    transform: translateY(22px);
  }
  .tile-symbol,
  .hub-icon,
  .agent-mark {
    display: grid;
    place-items: center;
    flex: 0 0 auto;
    color: #1769d5;
    background: #eaf4ff;
    border: 1px solid #cbe2fa;
    border-radius: 10px;
  }
  .tile-symbol {
    width: 42px;
    height: 42px;
  }
  .tile-body {
    display: grid;
    gap: 3px;
  }
  .tile-body small,
  .project-hub small {
    color: #7187a0;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .tile-body strong {
    font-size: 16px;
    line-height: 1.15;
  }
  .tile-body span {
    color: #5a7593;
    font-size: 14px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 125px;
  }
  .tile-stats {
    grid-column: 1 / -1;
    justify-content: space-between;
    padding-top: 13px;
    border-top: 1px solid #e4eef8;
    font-size: 14px;
    color: #55718e;
  }
  .tile-stats b {
    color: #173557;
  }
  .tile-stats em {
    color: #a45518;
    font-style: normal;
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .waiting-count {
    color: #7a6644;
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .project-map {
    width: 1000px;
    min-height: 560px;
    position: relative;
  }
  .connectors {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
  }
  .connectors path {
    fill: none;
    stroke: #91b9e4;
    stroke-width: 2;
    stroke-dasharray: 5 7;
  }
  .project-hub {
    position: absolute;
    top: 70px;
    left: 382px;
    width: 236px;
    height: 105px;
    box-sizing: border-box;
    display: flex;
    gap: 13px;
    align-items: center;
    border: 1px solid #80b3ea;
    border-radius: 14px;
    background: #fff;
    padding: 18px;
    box-shadow: 0 10px 24px #1769d51e;
  }
  .hub-icon {
    width: 48px;
    height: 48px;
  }
  .project-hub div {
    display: grid;
    gap: 3px;
  }
  .project-hub strong {
    font-size: 16px;
  }
  .project-hub span {
    color: #597693;
    font-size: 14px;
  }
  .agent-node {
    position: absolute;
    width: 250px;
    min-height: 153px;
    display: grid;
    gap: 10px;
    padding: 15px;
    text-align: left;
    color: #173557;
    border: 1px solid #b7d1eb;
    border-radius: 13px;
    background: #fff;
    box-shadow: 0 8px 18px #24517b12;
    cursor: pointer;
  }
  .agent-node:hover,
  .agent-node.selected {
    border-color: #1769d5;
    box-shadow:
      0 0 0 3px #b7dbff,
      0 12px 24px #24517b23;
  }
  .agent-node strong {
    font-size: 14px;
    line-height: 1.25;
    overflow: hidden;
    display: -webkit-box;
    line-clamp: 2;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }
  .agent-mark {
    width: 30px;
    height: 30px;
  }
  .node-status {
    margin-left: auto;
    font-size: 14px;
    font-weight: 700;
    color: #51708f;
    display: flex;
    align-items: center;
    gap: 5px;
  }
  .node-status i {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #287ee3;
  }
  .status-awaiting_review .node-status i {
    background: #d1841b;
  }
  .status-failed .node-status i,
  .status-blocked_quota .node-status i,
  .status-parked .node-status i {
    background: #ce5b42;
  }
  .status-merged .node-status i {
    background: #28a06b;
  }
  .node-meta {
    color: #69819a;
    font-size: 14px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .stage-track {
    display: flex;
    gap: 4px;
    margin-top: auto;
  }
  .stage-track i {
    height: 4px;
    flex: 1;
    border-radius: 3px;
    background: #dce8f4;
  }
  .stage-track i.active {
    background: #1769d5;
    box-shadow: 0 0 0 2px #cce5ff;
  }
  .waiting {
    color: #7a6644;
    font-size: 14px;
    font-weight: 600;
    background: #fff8e8;
    width: max-content;
    padding: 5px 7px;
    border-radius: 6px;
  }
  .empty-canvas {
    min-height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    color: #5e7895;
  }
  .empty-canvas h2 {
    color: #173557;
    margin: 19px 0 7px;
    font-size: 20px;
  }
  .empty-canvas p {
    margin: 0 0 22px;
    font-size: 14px;
  }
  .empty-orbit {
    width: 74px;
    height: 74px;
    display: grid;
    place-items: center;
    border: 1px dashed #77a8dd;
    border-radius: 50%;
    color: #1769d5;
    background: #f4f9ff;
  }
  .preview-note {
    margin: 10px 4px 0;
    color: #68819b;
    font-size: 12px;
  }
  @media (max-width: 760px) {
    .canvas-header {
      align-items: flex-start;
      flex-direction: column;
      padding-bottom: 10px;
    }
    .canvas-actions {
      width: 100%;
      justify-content: space-between;
    }
    .canvas-viewport {
      height: 560px;
      max-height: calc(100vh - 250px);
    }
    .portfolio-map {
      min-width: 0;
      min-height: 0;
      padding: 20px;
      grid-template-columns: 1fr;
      align-content: start;
      gap: 14px;
    }
    .project-tile,
    .project-tile.tile-1,
    .project-tile.tile-3,
    .project-tile.tile-4 {
      min-height: 150px;
      transform: none;
    }
    .project-map {
      width: 100%;
      min-height: 0;
      height: auto !important;
      padding: 24px 18px 28px 42px;
      display: flex;
      flex-direction: column;
      align-items: stretch;
      gap: 16px;
    }
    .project-map::before {
      content: '';
      position: absolute;
      z-index: 0;
      top: 96px;
      bottom: 44px;
      left: 31px;
      border-left: 2px dashed #91b9e4;
    }
    .connectors {
      display: none;
    }
    .project-hub,
    .agent-node {
      position: relative;
      z-index: 1;
      top: auto !important;
      left: auto !important;
      width: 100%;
      box-sizing: border-box;
    }
    .project-hub {
      height: auto;
      min-height: 94px;
      transform: none;
    }
    .agent-node {
      min-height: 145px;
    }
  }
</style>
