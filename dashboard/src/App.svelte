<script lang="ts">
  import { onMount, tick } from 'svelte';
  import Icon from './lib/Icon.svelte';
  import RunList from './lib/RunList.svelte';
  import MachinePanel from './lib/MachinePanel.svelte';
  import QuotaPanel from './lib/QuotaPanel.svelte';
  import RunInspector from './lib/RunInspector.svelte';
  import ActivityMonitor from './lib/ActivityMonitor.svelte';
  import { previewActivity } from './lib/activity';
  import { api, actions, ApiError, getToken, setToken } from './lib/api';
  import { demoMachine, demoProjects, demoQuota, demoRuns } from './lib/demo';
  import { activeStatuses } from './lib/format';
  import type {
    ActivitySnapshot,
    Machine,
    Project,
    QuotaResponse,
    RunDetail,
    RunSummary,
    RunsResponse,
  } from './lib/types';

  const navigation = [
    { id: 'overview', label: 'Overview', icon: 'overview' },
    { id: 'agents', label: 'Agents', icon: 'agent' },
    { id: 'projects', label: 'Projects', icon: 'projects' },
    { id: 'review', label: 'Review', icon: 'review' },
    { id: 'machines', label: 'Virtual machine', icon: 'vm' },
    { id: 'activity', label: 'Activity', icon: 'activity' },
    { id: 'quotas', label: 'Usage', icon: 'quota' },
  ];
  let page = $state('overview');
  let demo = $state(true);
  let runs = $state<RunSummary[]>(structuredClone(demoRuns));
  let projects = $state<Project[]>(structuredClone(demoProjects));
  let quota = $state<QuotaResponse>(structuredClone(demoQuota));
  let machine = $state<Machine | null>(structuredClone(demoMachine));
  let machineError = $state('');
  let projectsError = $state('');
  let total = $state(demoRuns.length);
  let loading = $state(false);
  let loadError = $state('');
  let notice = $state('');
  let syncedAt = $state('');
  let activity = $state<ActivitySnapshot | null>(null);
  let activityError = $state('');
  let activityFetchedAt = $state(0);
  let refreshActivity = $state<() => void>(() => {});
  let activityData = $derived(demo ? previewActivity(runs) : activity);
  let filter = $state('all');
  let projectFilter = $state('all');
  let search = $state('');
  let selected = $state<RunSummary | null>(null);
  let sidebarOpen = $state(false);
  let isMobile = $state(false);
  let sidebar: HTMLElement;
  let menuButton: HTMLButtonElement;
  let commandInput = $state<HTMLInputElement>();
  let modal = $state<'connect' | 'command' | 'task' | 'project' | null>(null);
  let dialog: HTMLDialogElement;
  let inspector: HTMLDialogElement;
  let tokenInput = $state('');
  let formError = $state('');
  let busy = $state(false);
  let command = $state('');
  let taskTitle = $state('');
  let taskBody = $state('');
  let taskProject = $state('');
  let repoInput = $state('');
  let slugInput = $state('');
  let session = $state(0);
  let refreshCount = 0;
  let refreshCompletion: Promise<void> = Promise.resolve();
  let activeRuns = $derived(runs.filter((run) => activeStatuses.includes(run.status)));
  let reviewRuns = $derived(runs.filter((run) => run.status === 'awaiting_review'));
  let filteredRuns = $derived(
    runs.filter(
      (run) =>
        (page !== 'review' || run.status === 'awaiting_review') &&
        (filter === 'all' ||
          (filter === 'active'
            ? activeStatuses.includes(run.status)
            : filter === 'completed'
              ? run.status === 'merged'
              : filter === 'attention'
                ? ['awaiting_review', 'parked', 'failed', 'blocked_quota'].includes(run.status)
                : run.status === filter)) &&
        (projectFilter === 'all' || run.project_slug === projectFilter) &&
        `${run.issue_title} ${run.project_slug} #${run.issue_number}`
          .toLowerCase()
          .includes(search.toLowerCase()),
    ),
  );
  let commandRuns = $derived(
    runs
      .filter((run) =>
        `${run.issue_title} ${run.project_slug}`.toLowerCase().includes(command.toLowerCase()),
      )
      .slice(0, 5),
  );
  let currentNav = $derived(navigation.find((item) => item.id === page));
  let selectedProject = $derived(projects.find((project) => project.id === taskProject));
  let issueUrl = $derived(
    selectedProject
      ? `https://github.com/${encodeURIComponent(selectedProject.owner)}/${encodeURIComponent(selectedProject.repo)}/issues/new?${new URLSearchParams({ title: taskTitle, body: taskBody, labels: 'werft:ready' })}`
      : '',
  );

  function navigate(id: string) {
    page = id;
    filter = 'all';
    search = '';
    projectFilter = 'all';
    if (sidebarOpen) void toggleSidebar(false);
    modal = null;
  }
  function openModal(value: typeof modal) {
    if (sidebarOpen) void toggleSidebar(false, false);
    formError = '';
    tokenInput = '';
    command = '';
    taskProject = projects[0]?.id ?? '';
    modal = value;
  }
  async function toggleSidebar(open: boolean, restoreFocus = true) {
    sidebarOpen = open;
    await tick();
    if (open) sidebar?.querySelector<HTMLButtonElement>('.nav-item.active, .nav-item')?.focus();
    else if (isMobile && restoreFocus) menuButton?.focus();
  }
  function openRun(run: RunSummary) {
    notice = '';
    modal = null;
    selected = run;
  }
  async function inspectActivityRun(id: string) {
    if (demo) {
      const run = runs.find((run) => run.id === id);
      if (run) openRun(run);
      return;
    }
    const requestSession = session;
    const run = await api<RunDetail>(`/runs/${encodeURIComponent(id)}`);
    if (requestSession === session) openRun(run);
  }
  function preview() {
    session++;
    refreshCount++;
    loading = false;
    localStorage.removeItem('werft_token');
    demo = true;
    runs = structuredClone(demoRuns);
    projects = structuredClone(demoProjects);
    quota = structuredClone(demoQuota);
    machine = structuredClone(demoMachine);
    total = runs.length;
    selected = null;
    modal = null;
    loadError = '';
    machineError = '';
    projectsError = '';
    syncedAt = '';
    notice = '';
  }
  async function refresh(more = false) {
    if (demo) {
      notice = 'Preview is up to date. Connect a manager for live updates.';
      return;
    }
    if (loading) {
      await refreshCompletion;
      return refresh(more);
    }
    loading = true;
    let releaseRefresh = () => {};
    refreshCompletion = new Promise<void>((resolve) => {
      releaseRefresh = resolve;
    });
    const requestSession = session;
    const currentRefresh = ++refreshCount;
    try {
      const [runResult, quotaResult, projectResult, machineResult] = await Promise.allSettled([
        fetchRunPages(more),
        api<QuotaResponse>('/quota'),
        api<Project[]>('/projects'),
        api<Machine>('/system'),
      ]);
      if (requestSession !== session) return;
      const unauthorized = [runResult, quotaResult, projectResult, machineResult].some(
        (result) =>
          result.status === 'rejected' &&
          result.reason instanceof ApiError &&
          [401, 403].includes(result.reason.status),
      );
      if (unauthorized) {
        localStorage.removeItem('werft_token');
        modal = 'connect';
        formError = 'Your connection has expired. Enter a valid manager token.';
        loadError = 'Connection expired. Displayed data may be out of date.';
        return;
      }
      if (runResult.status === 'fulfilled') {
        runs = more
          ? [
              ...runs,
              ...runResult.value.runs.filter(
                (run) => !runs.some((existing) => existing.id === run.id),
              ),
            ]
          : runResult.value.runs;
        total = runResult.value.total;
        if (selected) selected = runs.find((run) => run.id === selected?.id) ?? selected;
      }
      if (quotaResult.status === 'fulfilled') quota = quotaResult.value;
      loadError =
        runResult.status === 'rejected' || quotaResult.status === 'rejected'
          ? 'Could not refresh workspace data. Check your manager connection and retry. Displayed data may be out of date.'
          : '';
      if (projectResult.status === 'fulfilled') {
        projects = projectResult.value;
        projectsError = '';
      } else projectsError = 'Projects could not be loaded. Refresh to retry.';
      if (machineResult.status === 'fulfilled') {
        machine = machineResult.value;
        machineError = '';
      } else {
        machine = null;
        machineError =
          'Docker host could not be reached. Check the manager’s Docker connection and retry.';
      }
      if (!loadError)
        syncedAt = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } finally {
      if (currentRefresh === refreshCount) loading = false;
      releaseRefresh();
    }
  }
  async function fetchRunPages(more: boolean): Promise<RunsResponse> {
    if (more) return api<RunsResponse>(`/runs?limit=200&offset=${runs.length}`);
    const pages = await Promise.all(
      Array.from({ length: Math.max(1, Math.ceil(runs.length / 200)) }, (_, index) =>
        api<RunsResponse>(`/runs?limit=200&offset=${index * 200}`),
      ),
    );
    return {
      total: pages[0].total,
      runs: [...new Map(pages.flatMap((page) => page.runs).map((run) => [run.id, run])).values()],
    };
  }
  async function connect(event: SubmitEvent) {
    event.preventDefault();
    busy = true;
    formError = '';
    try {
      const value = tokenInput.trim();
      await api<QuotaResponse>('/quota', { headers: { Authorization: `Bearer ${value}` } });
      session++;
      setToken(value);
      tokenInput = '';
      demo = false;
      runs = [];
      projects = [];
      quota = { accounts: [] };
      machine = null;
      total = 0;
      selected = null;
      modal = null;
      notice = '';
      await refresh();
    } catch (err) {
      formError =
        err instanceof ApiError && [401, 403].includes(err.status)
          ? 'The manager did not accept this token. Check it and try again.'
          : 'The manager could not be reached. Check that it is running and retry.';
    } finally {
      busy = false;
    }
  }
  async function runAction(action: keyof typeof actions, run: RunSummary) {
    if (busy) return;
    busy = true;
    notice = '';
    try {
      if (demo) {
        const status = (
          { accept: 'merging', reject: 'parked', cancel: 'canceled', requeue: 'queued' } as const
        )[action];
        runs = runs.map((item) =>
          item.id === run.id ? { ...item, status, updated_at: new Date().toISOString() } : item,
        );
        selected = runs.find((item) => item.id === run.id) ?? null;
        if (action === 'cancel' && machine)
          machine = {
            ...machine,
            containers: machine.containers.filter((item) => item.run_id !== run.id),
          };
        notice = 'Preview updated. No live environment was changed.';
      } else {
        await actions[action](run.id);
        await refresh();
        notice = 'Run updated.';
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        notice = 'This run changed state. The workspace has been refreshed.';
        await refresh();
      } else if (err instanceof ApiError && [401, 403].includes(err.status)) {
        selected = null;
        openModal('connect');
        formError = 'Reconnect to continue.';
      } else notice = 'The action failed. Please retry.';
    } finally {
      busy = false;
    }
  }
  async function onboard(event: SubmitEvent) {
    event.preventDefault();
    busy = true;
    formError = '';
    const [owner, repo] = repoInput
      .trim()
      .replace(/^https:\/\/github\.com\//, '')
      .replace(/\/$/, '')
      .split('/');
    if (!owner || !repo || !/^[\w.-]+$/.test(owner) || !/^[\w.-]+$/.test(repo)) {
      formError = 'Enter a GitHub repository in owner/repository format.';
      busy = false;
      return;
    }
    try {
      if (demo)
        projects = [
          ...projects,
          {
            id: crypto.randomUUID(),
            slug: slugInput.trim(),
            owner,
            repo,
            lifecycle: 'bootstrap',
            created_at: new Date().toISOString(),
            onboarded_at: null,
          },
        ];
      else
        await api('/projects/onboard', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ slug: slugInput.trim(), owner, repo }),
        });
      modal = null;
      repoInput = '';
      slugInput = '';
      navigate('projects');
      if (!demo) await refresh();
      notice = demo ? 'Project added to this preview.' : 'Project onboarded.';
    } catch (err) {
      formError =
        err instanceof ApiError && err.status === 409
          ? 'This project is already onboarded.'
          : 'Could not onboard this repository. Check the GitHub App access and try again.';
    } finally {
      busy = false;
    }
  }
  function createPreviewTask(event: SubmitEvent) {
    event.preventDefault();
    if (!demo || !selectedProject || !taskTitle.trim()) return;
    runs = [
      {
        ...structuredClone(demoRuns[4]),
        id: crypto.randomUUID(),
        project_slug: selectedProject.slug,
        issue_title: taskTitle.trim(),
        issue_number: 129 + runs.length,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
      ...runs,
    ];
    total = runs.length;
    taskTitle = '';
    taskBody = '';
    modal = null;
    navigate('agents');
    notice = 'Task queued in the preview. No GitHub issue was created.';
  }
  function keydown(event: KeyboardEvent) {
    if (isMobile && sidebarOpen) {
      if (event.key === 'Escape') {
        event.preventDefault();
        void toggleSidebar(false);
        return;
      }
      if (event.key === 'Tab') {
        const controls = [
          ...sidebar.querySelectorAll<HTMLElement>('a[href], button:not(:disabled)'),
        ];
        const first = controls[0];
        const last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      if (selected) selected = null;
      openModal('command');
    }
  }
  onMount(() => {
    const media = matchMedia('(max-width: 760px)');
    const updateMobile = () => {
      isMobile = media.matches;
      if (!isMobile) sidebarOpen = false;
    };
    updateMobile();
    media.addEventListener('change', updateMobile);
    if (getToken()) {
      demo = false;
      runs = [];
      projects = [];
      quota = { accounts: [] };
      machine = null;
      total = 0;
      void refresh();
    }
    return () => media.removeEventListener('change', updateMobile);
  });
  $effect(() => {
    if (demo) return;
    const timer = setInterval(() => {
      if (!document.hidden && getToken()) void refresh();
    }, 10_000);
    return () => clearInterval(timer);
  });
  $effect(() => {
    const requestSession = session;
    activity = null;
    activityFetchedAt = 0;
    activityError = '';
    if (demo) return;
    const controller = new AbortController();
    let pending = false;
    async function poll() {
      if (pending || document.hidden || !getToken() || controller.signal.aborted) return;
      pending = true;
      try {
        const snapshot = await api<ActivitySnapshot>('/activity', {
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(6000)]),
        });
        if (controller.signal.aborted || requestSession !== session) return;
        if (!snapshot.manager || !Array.isArray(snapshot.active_runs))
          throw new Error('Invalid activity response');
        activity = snapshot;
        activityFetchedAt = Date.now();
        activityError = '';
      } catch (err) {
        if (controller.signal.aborted || requestSession !== session) return;
        if (err instanceof ApiError && [401, 403].includes(err.status)) {
          localStorage.removeItem('werft_token');
          modal = 'connect';
          formError = 'Your connection has expired. Enter a valid manager token.';
          activityError = 'Connection expired. Reconnect to resume live activity.';
        } else
          activityError =
            err instanceof ApiError && err.status === 404
              ? 'This manager does not expose activity yet. Update the manager to enable live monitoring.'
              : 'Backend activity could not be refreshed. Check the manager connection.';
      } finally {
        pending = false;
      }
    }
    refreshActivity = () => {
      void poll();
    };
    void poll();
    const timer = setInterval(() => void poll(), 3000);
    const resume = () => {
      if (!document.hidden) {
        void poll();
        void refresh();
      }
    };
    document.addEventListener('visibilitychange', resume);
    return () => {
      controller.abort();
      clearInterval(timer);
      document.removeEventListener('visibilitychange', resume);
    };
  });
  $effect(() => {
    if (!dialog) return;
    if (modal) {
      if (!dialog.open) dialog.showModal();
      if (modal === 'command') void tick().then(() => commandInput?.focus());
    } else dialog.close();
  });
  $effect(() => {
    if (!inspector) return;
    if (selected) {
      if (!inspector.open) inspector.showModal();
    } else inspector.close();
  });
</script>

<svelte:window onkeydown={keydown} />
<svelte:head
  ><title>{currentNav?.label ?? 'Settings'} · Werft</title><meta
    name="theme-color"
    content="#f7f9fc"
  /></svelte:head
>

<div class="app-shell">
  {#if sidebarOpen}<button
      class="sidebar-backdrop"
      aria-label="Close navigation"
      tabindex="-1"
      onclick={() => toggleSidebar(false)}
    ></button>{/if}
  <aside
    class="sidebar"
    class:open={sidebarOpen}
    bind:this={sidebar}
    inert={isMobile && !sidebarOpen}
    aria-label="Workspace navigation"
  >
    <a class="brand" href="#overview" onclick={() => navigate('overview')} aria-label="Werft home"
      ><svg viewBox="0 0 32 32" width="30" height="30" fill="none" aria-hidden="true"
        ><path
          d="m3 7 6 18 7-12 7 12 6-18 M10 7l6 10 6-10"
          stroke="currentColor"
          stroke-width="2.8"
          stroke-linejoin="round"
        /></svg
      ><span>werft<span class="brand-period">.</span></span><small>OS</small></a
    >
    <button class="workspace-switch" onclick={() => openModal('connect')}
      ><span class="workspace-avatar">W</span><span
        ><strong>Personal workspace</strong><small
          >{demo ? 'Preview environment' : 'Connected manager'}</small
        ></span
      ><Icon name="down" size={14} /></button
    >
    <button class="search-trigger" onclick={() => openModal('command')}
      ><Icon name="search" size={16} /><span>Jump to anything</span><kbd>Ctrl K</kbd></button
    >
    <nav aria-label="Main navigation">
      {#each navigation as item, index (item.id)}{#if index === 4}<div
            class="nav-divider"
          ></div>{/if}<button
          class="nav-item"
          class:active={page === item.id}
          aria-current={page === item.id ? 'page' : undefined}
          onclick={() => navigate(item.id)}
          ><Icon name={item.icon} size={19} /><span>{item.label}</span
          >{#if item.id === 'review' && reviewRuns.length}<span class="nav-count"
              >{reviewRuns.length}</span
            >{:else if item.id === 'agents' && activeRuns.length}<span class="nav-dot"
            ></span>{/if}</button
        >{/each}
    </nav>
    <div class="sidebar-bottom">
      <button
        class="nav-item"
        class:active={page === 'settings'}
        onclick={() => navigate('settings')}
        ><Icon name="settings" size={19} /><span>Settings</span></button
      ><button class="profile-button" onclick={() => navigate('settings')}
        ><span class="profile-avatar">OP</span><span
          ><strong>Operator</strong><small>Personal account</small></span
        ><Icon name="down" size={15} /></button
      >
    </div>
  </aside>

  <div class="workspace-main" inert={isMobile && sidebarOpen}>
    <header class="topbar">
      <div class="breadcrumbs">
        <button
          class="icon-button mobile-menu"
          bind:this={menuButton}
          aria-label="Open navigation"
          onclick={() => toggleSidebar(true)}><Icon name="menu" /></button
        ><span>Workspace</span><Icon name="chevron" size={13} /><strong
          >{currentNav?.label ?? 'Settings'}</strong
        >
      </div>
      <div class="topbar-right">
        <button
          class="connection-state"
          onclick={() => navigate('activity')}
          title="Open backend activity"
          ><span
            class="live-dot"
            class:preview-dot={demo || !!activityError || !activity?.manager.available}
          ></span>{demo
            ? 'Preview mode'
            : activityError || loadError
              ? 'Updates interrupted'
              : !activity
                ? 'Connecting'
                : !activity.manager.available
                  ? 'Scheduler unavailable'
                  : activity.manager.live_driver_run_ids.length
                    ? `${activity.manager.live_driver_run_ids.length} active agent sessions`
                    : 'Watching for work'}</button
        ><span class="topbar-divider"></span><button
          class="icon-button"
          aria-label="Refresh workspace"
          title="Refresh workspace"
          disabled={loading}
          onclick={() => {
            refreshActivity();
            void refresh();
          }}><span class:spinning={loading}><Icon name="refresh" size={16} /></span></button
        ><button
          class="topbar-avatar"
          aria-label="Account settings"
          onclick={() => navigate('settings')}>OP</button
        >
      </div>
    </header>
    {#if demo}<div class="preview-banner">
        <span
          ><Icon name="spark" size={14} /><strong>Demo workspace</strong><span
            class="preview-description"
            >Showing sample tasks and machines. Connect a manager to view live activity.</span
          ></span
        ><button onclick={() => openModal('connect')}
          >Connect manager<Icon name="arrow" size={14} /></button
        >
      </div>{/if}
    {#if loadError}<div class="notice warning" role="alert">
        <Icon name="warning" size={17} /><span>{loadError}</span><button
          class="text-button"
          onclick={() => refresh()}>Retry</button
        >
      </div>{/if}
    {#if notice && !selected}<div class="toast" role="status">
        <Icon name="check" size={16} /><span>{notice}</span><button
          class="icon-button"
          aria-label="Dismiss notification"
          onclick={() => (notice = '')}><Icon name="close" size={15} /></button
        >
      </div>{/if}

    <main id="main-content" class="page-content">
      {#if page === 'overview'}
        <div class="page-heading">
          <div>
            <h1>Overview</h1>
          </div>
          <button
            class="button"
            class:primary={!reviewRuns.length}
            onclick={() => openModal('task')}><Icon name="plus" size={17} />New task</button
          >
        </div>
        <div class="overview-layout">
          <div class="overview-work">
            {#if reviewRuns.length}
              <div class="section-heading attention-heading">
                <h2>Needs your attention <span class="count-label">{reviewRuns.length}</span></h2>
              </div>
              <button class="review-callout" onclick={() => openRun(reviewRuns[0])}>
                <span class="review-callout-icon"><Icon name="review" size={23} /></span>
                <span class="review-task"
                  ><strong>{reviewRuns[0].issue_title}</strong><small
                    >{reviewRuns[0].project_slug} · {reviewRuns.length}
                    {reviewRuns.length === 1 ? 'task is' : 'tasks are'} ready for your review.</small
                  ></span
                >
                <span class="review-link">Review work<Icon name="arrow" size={16} /></span>
              </button>
            {/if}
            <ActivityMonitor
              data={activityData}
              error={activityError}
              fetchedAt={activityFetchedAt}
              {demo}
              compact
              oninspect={inspectActivityRun}
              onrefresh={refreshActivity}
              onexpand={() => navigate('activity')}
            />
          </div>
          <aside class="overview-insights">
            <MachinePanel
              {machine}
              error={machineError}
              compact
              {demo}
              onmanage={() => navigate('machines')}
            /><QuotaPanel accounts={quota.accounts} compact />
          </aside>
        </div>
      {:else if page === 'activity'}
        <div class="page-heading simple">
          <div>
            <h1>Activity</h1>
          </div>
          <button class="button" onclick={refreshActivity}
            ><Icon name="refresh" size={16} />Refresh activity</button
          >
        </div>
        <ActivityMonitor
          data={activityData}
          error={activityError}
          fetchedAt={activityFetchedAt}
          {demo}
          oninspect={inspectActivityRun}
          onrefresh={refreshActivity}
        />
      {:else if page === 'agents' || page === 'review'}
        <div class="page-heading simple">
          <div>
            <h1>
              {page === 'agents' ? 'Agents' : 'Review'}
            </h1>
          </div>
          <button class="button primary" onclick={() => openModal('task')}
            ><Icon name="plus" size={17} />New task</button
          >
        </div>
        <div class="list-toolbar">
          <div class="filter-tabs">
            {#each page === 'review' ? ['all'] : ['all', 'active', 'attention', 'completed'] as item (item)}<button
                class:active={filter === item}
                onclick={() => (filter = item)}
                >{item === 'all'
                  ? 'All tasks'
                  : item === 'attention'
                    ? 'Needs attention'
                    : item.charAt(0).toUpperCase() + item.slice(1)}</button
              >{/each}
          </div>
          <div class="list-controls">
            <label class="search-field"
              ><Icon name="search" size={16} /><input
                aria-label="Search tasks"
                placeholder="Search tasks…"
                bind:value={search}
              /></label
            ><select aria-label="Filter by project" bind:value={projectFilter}
              ><option value="all">All projects</option
              >{#each projects as project (project.id)}<option value={project.slug}
                  >{project.slug}</option
                >{/each}</select
            >
          </div>
        </div>
        <RunList
          runs={filteredRuns}
          onselect={openRun}
          empty={search || filter !== 'all' || projectFilter !== 'all'
            ? 'No tasks match these filters'
            : page === 'review'
              ? 'You’re all caught up'
              : 'No tasks found'}
        />
        <div class="list-footer">
          <span>{filteredRuns.length} matching · {runs.length} of {total} runs loaded</span
          >{#if runs.length < total}<button
              class="button"
              disabled={loading}
              onclick={() => refresh(true)}>Load more</button
            >{/if}<span>{demo ? 'Sample data' : 'Refreshes every 10 seconds'}</span>
        </div>
      {:else if page === 'machines'}
        <div class="page-heading simple">
          <div>
            <h1>Virtual machine</h1>
          </div>
          <button class="button" disabled={loading} onclick={() => refresh()}
            ><Icon name="refresh" size={16} />Refresh host</button
          >
        </div>
        <div class="machine-page-grid">
          <MachinePanel
            {machine}
            error={machineError}
            {demo}
            {runs}
            onselect={openRun}
            onrefresh={() => refresh()}
          />
          <section class="environment-policy">
            <div class="section-heading">
              <h2>How your VM works</h2>
              <Icon name="shield" />
            </div>
            <div class="policy-item">
              <span class="policy-icon"><Icon name="vm" /></span>
              <h3>One dedicated host</h3>
              <p>
                Your manager orchestrates the Docker host you connect. Agent environments are
                created and cleaned up with each run.
              </p>
            </div>
            <div class="policy-item">
              <span class="policy-icon"><Icon name="terminal" /></span>
              <h3>Agent environments</h3>
              <p>
                Agents can install tools, build applications, and run services inside their
                disposable environments.
              </p>
            </div>
            <div class="policy-item">
              <span class="policy-icon"><Icon name="file" /></span>
              <h3>Run evidence</h3>
              <p>
                Transcripts, diffs, and collected artifacts remain attached to the run after its
                environment is gone.
              </p>
            </div>
            <button class="button full" onclick={() => navigate('agents')}
              >Inspect active workloads<Icon name="arrow" size={16} /></button
            >
          </section>
        </div>
      {:else if page === 'projects'}
        <div class="page-heading simple">
          <div>
            <h1>Projects</h1>
          </div>
          <button class="button primary" onclick={() => openModal('project')}
            ><Icon name="plus" size={17} />Add project</button
          >
        </div>
        {#if projectsError}<p class="notice warning" role="alert">{projectsError}</p>{/if}
        <div class="project-list">
          {#each projects as project (project.id)}{@const projectRuns = runs.filter(
              (run) => run.project_slug === project.slug,
            )}
            <article class="project-entry">
              <span class="project-symbol"><Icon name="projects" size={23} /></span>
              <div>
                <h2>{project.slug}</h2>
                <p>{project.owner}/{project.repo}</p>
                <div class="project-info">
                  <span
                    ><Icon name="shield" size={14} />{project.lifecycle === 'oracle_gated'
                      ? 'CI verified merges'
                      : 'Human review'}</span
                  ><span
                    >{projectRuns.filter((run) => activeStatuses.includes(run.status)).length} active
                    · {projectRuns.length} loaded runs</span
                  >
                </div>
              </div>
              <button
                class="button"
                onclick={() => {
                  navigate('agents');
                  projectFilter = project.slug;
                }}>Open agents<Icon name="arrow" size={16} /></button
              >
            </article>{:else}<div class="empty-state">
              <Icon name="projects" size={32} />
              <h3>No projects connected</h3>
              <p>Connect a GitHub repository to bring its approved work into Werft.</p>
              <button class="button primary" onclick={() => openModal('project')}
                >Add your first project</button
              >
            </div>{/each}
        </div>
      {:else if page === 'quotas'}
        <div class="page-heading simple">
          <div>
            <h1>Usage</h1>
          </div>
          <button class="button" onclick={() => refresh()} disabled={loading}
            ><Icon name="refresh" size={16} />Refresh usage</button
          >
        </div>
        <div class="quota-page-grid">
          <QuotaPanel accounts={quota.accounts} />
          <section class="quota-explainer">
            <h2>Capacity, accounted for.</h2>
            <dl>
              <div>
                <dt>Consumed</dt>
                <dd>Provider time already used in the rolling quota window.</dd>
              </div>
              <div>
                <dt>Reserved</dt>
                <dd>Capacity held for work that is already in flight.</dd>
              </div>
              <div>
                <dt>Headroom</dt>
                <dd>Time available for new work under your configured ceiling.</dd>
              </div>
            </dl>
            <p>
              Quota configuration lives on your manager. Provider readings and the manager’s ledger
              determine whether another run can start.
            </p>
          </section>
        </div>
      {:else if page === 'settings'}
        <div class="page-heading simple">
          <div>
            <h1>Settings</h1>
          </div>
        </div>
        <section class="settings-section">
          <h2>Manager connection</h2>
          <div class="settings-row">
            <div>
              <strong>{demo ? 'Preview workspace' : 'Connected to this manager'}</strong>
              <p>
                {demo
                  ? 'Explore sample data, or connect to your own agent infrastructure.'
                  : 'Requests use a bearer token over this app’s same-origin API.'}
              </p>
            </div>
            <button class="button primary" onclick={() => openModal('connect')}
              >{demo ? 'Connect manager' : 'Update token'}</button
            >
          </div>
          {#if !demo}<div class="settings-row">
              <div>
                <strong>Disconnect this browser</strong>
                <p>Remove the saved token. Your running agents continue on the manager.</p>
              </div>
              <button class="button" onclick={preview}
                ><Icon name="logout" size={16} />Disconnect</button
              >
            </div>{/if}
          <h2>Workspace behavior</h2>
          <div class="settings-row">
            <div>
              <strong>Live updates</strong>
              <p>Refreshes every 10 seconds while this tab is visible.</p>
            </div>
            <span class="status status-running">Enabled</span>
          </div>
          <div class="settings-row">
            <div>
              <strong>Command menu</strong>
              <p>Navigate anywhere or find a task without leaving the keyboard.</p>
            </div>
            <kbd>Ctrl / ⌘ K</kbd>
          </div>
          <div class="settings-row">
            <div>
              <strong>Reduced motion</strong>
              <p>Follows your operating system’s accessibility preference.</p>
            </div>
            <span class="muted">System</span>
          </div>
        </section>
      {/if}
    </main>
    <footer class="statusbar">
      <span
        ><span class="live-dot" class:preview-dot={demo}></span>{demo
          ? 'Preview environment · Sample data'
          : syncedAt
            ? `Last synced ${syncedAt}`
            : 'Connecting to manager'}</span
      >
    </footer>
  </div>
</div>

<dialog
  class="app-dialog"
  class:command-dialog={modal === 'command'}
  bind:this={dialog}
  onclose={() => (modal = null)}
  oncancel={() => (modal = null)}
  aria-labelledby="modal-title"
>
  <div class="dialog-heading">
    <h2 id="modal-title">
      {modal === 'connect'
        ? 'Connect manager'
        : modal === 'command'
          ? 'Jump to anything'
          : modal === 'project'
            ? 'Add a project'
            : 'Create task'}
    </h2>
    <button class="icon-button" aria-label="Close dialog" onclick={() => (modal = null)}
      ><Icon name="close" /></button
    >
  </div>
  {#if modal === 'connect'}<form onsubmit={connect} class="dialog-form">
      <p>Enter the API token from your Werft manager to connect this dashboard.</p>
      <label for="api-token">Manager API token</label><input
        id="api-token"
        type="password"
        required
        autocomplete="off"
        placeholder="Paste your token"
        bind:value={tokenInput}
      /><small>The token is saved in this browser. Disconnect in Settings to remove it.</small
      >{#if formError}<p class="form-error" role="alert">{formError}</p>{/if}<button
        class="button primary full"
        disabled={busy}
        >{busy ? 'Connecting…' : 'Connect manager'}<Icon name="arrow" size={16} /></button
      >
    </form>
  {:else if modal === 'command'}<div class="command-body">
      <label class="command-input"
        ><Icon name="search" size={20} /><input
          aria-label="Search commands and tasks"
          bind:this={commandInput}
          placeholder="Where would you like to go?"
          bind:value={command}
        /></label
      >
      <p class="command-label">Navigate</p>
      {#each navigation.filter((item) => item.label
          .toLowerCase()
          .includes(command.toLowerCase())) as item (item.id)}<button
          class="command-result"
          onclick={() => navigate(item.id)}
          ><Icon name={item.icon} /><span>{item.label}</span><Icon name="arrow" size={14} /></button
        >{/each}
      <p class="command-label">Tasks</p>
      {#each commandRuns as run (run.id)}<button class="command-result" onclick={() => openRun(run)}
          ><Icon name="agent" /><span>{run.issue_title}<small>{run.project_slug}</small></span><Icon
            name="chevron"
            size={14}
          /></button
        >{:else}<p class="muted empty-inline">No matching tasks.</p>{/each}
    </div>
    <div class="command-footer">
      <kbd>Esc</kbd><span>to close</span><span>Navigate with Tab · Open with Enter</span>
    </div>
  {:else if modal === 'project'}<form class="dialog-form" onsubmit={onboard}>
      <p>
        Werft will onboard the repository and establish its unattended branch. The GitHub App must
        have access to it.
      </p>
      <label for="project-repo">GitHub repository</label><input
        id="project-repo"
        placeholder="owner/repository"
        required
        bind:value={repoInput}
      /><label for="project-slug">Project name</label><input
        id="project-slug"
        placeholder="my-project"
        pattern="[a-z0-9][a-z0-9-]*"
        title="Lowercase letters, numbers, and hyphens"
        required
        bind:value={slugInput}
      />{#if formError}<p class="form-error" role="alert">{formError}</p>{/if}<button
        class="button primary full"
        disabled={busy}
        >{busy ? 'Onboarding…' : demo ? 'Add to preview' : 'Onboard project'}<Icon
          name="plus"
          size={16}
        /></button
      >
    </form>
  {:else if modal === 'task'}<form class="dialog-form" onsubmit={createPreviewTask}>
      <p>
        {demo
          ? 'Give your preview agents something to work on.'
          : 'Create an approved GitHub issue. Werft picks it up once you submit it with the werft:ready label.'}
      </p>
      <label for="task-project">Project</label><select
        id="task-project"
        bind:value={taskProject}
        required
        ><option value="" disabled>Select a project</option
        >{#each projects as project (project.id)}<option value={project.id}>{project.slug}</option
          >{/each}</select
      ><label for="task-title">What should the agent do?</label><input
        id="task-title"
        placeholder="Build, fix, explore…"
        required
        bind:value={taskTitle}
      /><label for="task-body"
        >Context and acceptance criteria <span class="muted">(optional)</span></label
      ><textarea
        id="task-body"
        rows="4"
        placeholder="Describe what a good result looks like."
        bind:value={taskBody}></textarea>{#if demo}<button
          class="button primary full"
          disabled={!projects.length}>Queue preview task<Icon name="arrow" size={16} /></button
        >{:else if issueUrl && taskTitle.trim()}<a
          class="button primary full"
          href={issueUrl}
          target="_blank"
          rel="noreferrer">Continue in GitHub<Icon name="external" size={16} /></a
        >{:else}<button class="button primary full" disabled type="button"
          >Continue in GitHub<Icon name="external" size={16} /></button
        >{/if}{#if !projects.length}<p class="form-error">
          Add a project before creating a task.
        </p>{/if}
    </form>{/if}
</dialog>
<dialog
  class="inspector-dialog"
  bind:this={inspector}
  onclose={() => (selected = null)}
  oncancel={() => (selected = null)}
  aria-label="Run details"
>
  {#if selected}<RunInspector
      run={selected}
      message={notice}
      {demo}
      {busy}
      onclose={() => (selected = null)}
      onaction={runAction}
    />{/if}
</dialog>
