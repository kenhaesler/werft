<script lang="ts">
  import { onMount } from 'svelte';
  import { SvelteURLSearchParams } from 'svelte/reactivity';
  import Icon from './Icon.svelte';
  import { api } from './api';
  import { eventLabel } from './activity';

  export interface EventItem {
    id: number;
    run_id: string;
    project_slug: string;
    issue_number: number;
    issue_title: string;
    run_status: string;
    event_type: string;
    phase: string | null;
    from_status: string | null;
    to_status: string | null;
    created_at: string;
    payload?: Record<string, unknown>;
  }

  let {
    demo = false,
    events: snapshotEvents = [],
    ontask,
  }: { demo?: boolean; events?: EventItem[]; ontask?: (runId: string) => void } = $props();
  let events = $state<EventItem[]>([]);
  let total = $state(0);
  let query = $state('');
  let appliedQuery = $state('');
  let page = $state(0);
  let loading = $state(false);
  let error = $state('');
  const pageSize = 6;
  let controller: AbortController | null = null;

  function label(event: EventItem): string {
    return eventLabel(event);
  }

  function shownEvents(): EventItem[] {
    if (!demo || !appliedQuery) return events;
    const needle = appliedQuery.toLowerCase();
    return events.filter((event) =>
      `${event.issue_title} ${event.project_slug} ${event.event_type}`
        .toLowerCase()
        .includes(needle),
    );
  }

  function visibleEvents(): EventItem[] {
    const filtered = shownEvents();
    return demo ? filtered.slice(page * pageSize, (page + 1) * pageSize) : filtered;
  }

  async function load() {
    if (demo) {
      events = snapshotEvents;
      total = shownEvents().length;
      return;
    }
    loading = true;
    error = '';
    controller?.abort();
    const request = new AbortController();
    controller = request;
    try {
      const params = new SvelteURLSearchParams({
        limit: String(pageSize),
        offset: String(page * pageSize),
      });
      if (appliedQuery) params.set('q', appliedQuery);
      const result = await api<{ total: number; events: EventItem[] }>(`/events?${params}`, {
        signal: AbortSignal.any([request.signal, AbortSignal.timeout(15_000)]),
      });
      if (request.signal.aborted || controller !== request) return;
      events = result.events;
      total = result.total;
    } catch {
      if (request.signal.aborted || controller !== request) return;
      error = 'Event history could not be loaded. Retry to reconnect.';
    } finally {
      if (controller === request) loading = false;
    }
  }

  function search() {
    appliedQuery = query.trim();
    page = 0;
    void load();
  }

  function changePage(next: number) {
    page = next;
    void load();
  }

  $effect(() => {
    if (demo) {
      events = snapshotEvents;
      const needle = appliedQuery.toLowerCase();
      total = needle
        ? snapshotEvents.filter((event) =>
            `${event.issue_title} ${event.project_slug} ${event.event_type}`
              .toLowerCase()
              .includes(needle),
          ).length
        : snapshotEvents.length;
    }
  });

  onMount(() => {
    if (!demo) void load();
    return () => controller?.abort();
  });
</script>

<section class="event-history" aria-labelledby="event-history-title">
  <div class="event-history-heading">
    <div><h2 id="event-history-title">Event history</h2></div>
    <button class="text-button" onclick={() => load()} disabled={loading}
      ><Icon name="refresh" size={15} />{error ? 'Retry' : 'Refresh'}</button
    >
  </div>
  <form
    class="event-history-search"
    onsubmit={(event) => {
      event.preventDefault();
      search();
    }}
  >
    <label
      ><Icon name="search" size={16} /><input
        aria-label="Search event history"
        maxlength={200}
        placeholder="Search tasks, projects, or changes"
        bind:value={query}
      /></label
    >
    <button class="button small" type="submit">Search</button>
  </form>
  {#if error}<p class="event-history-error" role="alert">{error}</p>{/if}
  {#if loading}<p class="event-history-empty">Loading event history…</p>
  {:else if visibleEvents().length}
    <ol class="event-history-list">
      {#each visibleEvents() as event (event.id)}
        <li>
          <details>
            <summary>
              <time datetime={event.created_at}>{new Date(event.created_at).toLocaleString()}</time>
              <span
                ><strong>{event.issue_title}</strong><small
                  >{event.project_slug} · #{event.issue_number}</small
                ></span
              >
              <span class="event-history-change">{label(event)}</span>
              <Icon name="down" size={15} />
            </summary>
            <div class="event-history-detail">
              <button class="text-button" onclick={() => ontask?.(event.run_id)}
                >Open task <Icon name="arrow" size={14} /></button
              >
              {#if event.payload}<pre>{JSON.stringify(event.payload, null, 2)}</pre>{/if}
            </div>
          </details>
        </li>
      {/each}
    </ol>
  {:else}<p class="event-history-empty">
      {appliedQuery ? 'No recorded events match this search.' : 'No recorded task events yet.'}
    </p>{/if}
  {#if total > pageSize}<div class="event-history-pages">
      <span>{page * pageSize + 1}–{Math.min((page + 1) * pageSize, total)} of {total}</span><span
        ><button
          class="button small"
          aria-label="Previous events"
          disabled={page === 0 || loading}
          onclick={() => changePage(page - 1)}>Previous</button
        ><button
          class="button small"
          aria-label="Next events"
          disabled={(page + 1) * pageSize >= total || loading}
          onclick={() => changePage(page + 1)}>Next</button
        ></span
      >
    </div>{/if}
</section>

<style>
  .event-history {
    min-width: 0;
    font-size: 14px;
  }
  .event-history-heading,
  .event-history-pages {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }
  h2 {
    margin: 0;
    font-size: 18px;
  }
  p {
    color: var(--muted);
    margin: 5px 0 0;
  }
  .event-history-search {
    display: flex;
    gap: 8px;
    margin: 18px 0 10px;
  }
  .event-history-search label {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1;
    border: 1px solid var(--border);
    border-radius: 7px;
    padding: 8px 10px;
  }
  input {
    border: 0;
    outline: 0;
    min-width: 0;
    width: 100%;
    font: inherit;
    color: var(--text);
  }
  .event-history-list {
    list-style: none;
    padding: 0;
    margin: 0;
    border-top: 1px solid var(--border);
  }
  li {
    border-bottom: 1px solid var(--border);
  }
  summary {
    display: grid;
    grid-template-columns: 150px minmax(0, 1fr) minmax(120px, 0.5fr) 15px;
    gap: 14px;
    align-items: center;
    cursor: pointer;
    list-style: none;
    padding: 12px 0;
  }
  summary::-webkit-details-marker {
    display: none;
  }
  time,
  small {
    color: var(--muted);
  }
  strong,
  small {
    display: block;
    overflow-wrap: anywhere;
  }
  small {
    margin-top: 3px;
  }
  .event-history-change {
    color: var(--muted);
  }
  .event-history-detail {
    padding: 0 0 14px 164px;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    background: var(--canvas, #f7f9fc);
    padding: 10px;
    margin: 10px 0 0;
    border-radius: 7px;
    font-size: 12px;
  }
  .event-history-pages {
    margin-top: 14px;
    color: var(--muted);
  }
  .event-history-error {
    color: var(--danger);
    padding: 10px 0;
  }
  .event-history-empty {
    padding: 18px 0;
  }
  @media (max-width: 600px) {
    summary {
      grid-template-columns: minmax(0, 1fr) 15px;
      gap: 6px 10px;
    }
    summary time,
    .event-history-change {
      grid-column: 1;
    }
    summary > span:nth-child(2) {
      grid-column: 1;
      grid-row: 2;
    }
    summary > span:nth-child(3) {
      grid-column: 1;
      grid-row: 3;
    }
    .event-history-detail {
      padding-left: 0;
    }
  }
</style>
