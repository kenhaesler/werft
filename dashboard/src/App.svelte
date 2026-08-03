<script lang="ts">
  import { getToken, setToken, api, actions, ApiError } from './lib/api';
  import type { RunSummary, RunsResponse, QuotaResponse } from './lib/types';
  import QuotaStrip from './lib/QuotaStrip.svelte';
  import RunRow from './lib/RunRow.svelte';

  const POLL_INTERVAL_MS = 10_000;

  let tokenInput = $state('');
  let authenticated = $state(false);
  let authError = $state('');
  let checkingToken = $state(false);

  let runs = $state<RunSummary[]>([]);
  let total = $state(0);
  let quota = $state<QuotaResponse | null>(null);
  let notice = $state('');
  let loadError = $state('');

  async function probeToken(token: string): Promise<boolean> {
    setToken(token);
    try {
      quota = await api<QuotaResponse>('/quota');
      return true;
    } catch {
      return false;
    }
  }

  async function refresh(): Promise<void> {
    try {
      const [runsRes, quotaRes] = await Promise.all([
        api<RunsResponse>('/runs?limit=50&offset=0'),
        api<QuotaResponse>('/quota'),
      ]);
      runs = runsRes.runs;
      total = runsRes.total;
      quota = quotaRes;
      loadError = '';
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        authenticated = false;
      } else {
        loadError = 'Failed to refresh runs from the manager.';
      }
    }
  }

  async function handleSubmit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    checkingToken = true;
    authError = '';
    const ok = await probeToken(tokenInput.trim());
    checkingToken = false;
    if (ok) {
      authenticated = true;
      await refresh();
    } else {
      authError = 'Invalid token, or the manager is unreachable.';
    }
  }

  async function runAction(action: (id: string) => Promise<void>, id: string): Promise<void> {
    try {
      await action(id);
      notice = '';
      await refresh();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        notice = 'That run changed state under you - refreshed the list.';
        await refresh();
      } else if (err instanceof ApiError && err.status === 401) {
        authenticated = false;
      } else {
        notice = 'Action failed. Please try again.';
      }
    }
  }

  // Silent one-shot check on load: if a token is already stored, try it
  // without showing the gate. This is a plain call (not an $effect) since it
  // only needs to run once at component init, not react to state changes.
  (async () => {
    const existing = getToken();
    if (existing) {
      const ok = await probeToken(existing);
      if (ok) {
        authenticated = true;
        await refresh();
      }
    }
  })();

  $effect(() => {
    if (!authenticated) return;
    const interval = setInterval(() => {
      void refresh();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  });
</script>

{#if !authenticated}
  <div class="token-gate">
    <form onsubmit={handleSubmit}>
      <label for="token-input">Werft API token</label>
      <input id="token-input" type="password" bind:value={tokenInput} autocomplete="off" />
      <button type="submit" disabled={checkingToken}>Connect</button>
      {#if authError}<p class="token-gate__error">{authError}</p>{/if}
    </form>
  </div>
{:else}
  <main class="dashboard">
    {#if quota}
      <QuotaStrip accounts={quota.accounts} />
    {/if}
    {#if notice}<p class="dashboard__notice">{notice}</p>{/if}
    {#if loadError}<p class="dashboard__error">{loadError}</p>{/if}
    <table class="runs-table">
      <thead>
        <tr>
          <th>State</th>
          <th>Project</th>
          <th>Issue</th>
          <th>Attempts</th>
          <th>Outcome</th>
          <th>Parked reason</th>
          <th>PR</th>
          <th>Artifacts</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {#each runs as run (run.id)}
          <RunRow
            {run}
            onAccept={() => runAction(actions.accept, run.id)}
            onReject={() => runAction(actions.reject, run.id)}
            onCancel={() => runAction(actions.cancel, run.id)}
            onRequeue={() => runAction(actions.requeue, run.id)}
          />
        {/each}
      </tbody>
    </table>
    <p class="dashboard__total">{runs.length} of {total} runs shown</p>
  </main>
{/if}
