import { expect, test, type Page } from '@playwright/test';

const now = () => new Date().toISOString();

function summary(id: string, title: string, status: 'queued' | 'running' | 'failed' = 'queued') {
  return {
    id,
    project_slug: 'platform',
    issue_number: id === 'loaded-run' ? 41 : 88,
    issue_title: title,
    status,
    attempt_count: 1,
    max_attempts: 3,
    latest_outcome: status === 'failed' ? 'timeout' : null,
    parked_reason: null,
    pr_number: null,
    pr_url: null,
    created_at: now(),
    updated_at: now(),
  };
}

function detail(id: string, title: string, status: 'queued' | 'running' | 'failed' = 'queued') {
  return {
    ...summary(id, title, status),
    branch_name: 'codex/activity-monitor',
    base_sha: null,
    merge_commit_sha: null,
    error_message: null,
    result: null,
    events: [],
    attempts: [],
    artifacts: [],
  };
}

function snapshot(version = 1) {
  const future = new Date(Date.now() + 60_000).toISOString();
  const past = new Date(Date.now() - 15_000).toISOString();
  return {
    generated_at: now(),
    manager: {
      available: true,
      started_at: past,
      workers: {
        tick: {
          state: 'running',
          current_operation: {
            kind: version === 1 ? 'lease_renewal' : 'dispatch_run',
            key: 'loaded-run',
          },
          last_started_at: past,
          last_completed_at: past,
          last_error_at: null,
          waiting_until: null,
        },
        issues: {
          state: 'waiting',
          current_operation: null,
          last_started_at: null,
          last_completed_at: past,
          last_error_at: null,
          waiting_until: future,
        },
        checks: {
          state: 'error',
          current_operation: null,
          last_started_at: past,
          last_completed_at: past,
          last_error_at: past,
          waiting_until: future,
        },
      },
      recent_operations: [],
      live_driver_run_ids: ['loaded-run'],
    },
    // These counts deliberately exceed active_runs: stages represent the whole workspace.
    status_counts: {
      queued: 5,
      running: version === 1 ? 3 : 4,
      awaiting_ci: 2,
      awaiting_review: 1,
      merging: 1,
      failed: 2,
      parked: 1,
      merged: 9,
    },
    recent_events: [
      {
        id: 701,
        run_id: 'event-only-run',
        project_slug: 'audit',
        issue_number: 88,
        issue_title: 'Inspect a run absent from the loaded list',
        run_status: 'failed',
        event_type: 'alert',
        phase: null,
        from_status: 'running',
        to_status: 'failed',
        created_at: past,
      },
    ],
    active_runs: [
      {
        run_id: 'loaded-run',
        project_slug: 'platform',
        issue_number: 41,
        issue_title: version === 1 ? 'Loaded task stays visible' : 'Snapshot refreshed live',
        status: 'running',
        provider: 'codex',
        container_id: 'container-1234567890',
        attempt_started_at: past,
        last_heartbeat_at: past,
        lease_expires_at: future,
        hard_deadline_at: future,
        next_attempt_at: future,
        parked_reason: null,
        updated_at: past,
      },
      {
        run_id: 'queued-active',
        project_slug: 'platform',
        issue_number: 42,
        issue_title: 'Only one queued task is loaded',
        status: 'queued',
        provider: null,
        container_id: null,
        attempt_started_at: null,
        last_heartbeat_at: null,
        lease_expires_at: null,
        hard_deadline_at: null,
        next_attempt_at: future,
        parked_reason: null,
        updated_at: past,
      },
      {
        run_id: 'failed-active',
        project_slug: 'platform',
        issue_number: 43,
        issue_title: 'Failed task in the active subset',
        status: 'failed',
        provider: null,
        container_id: null,
        attempt_started_at: past,
        last_heartbeat_at: null,
        lease_expires_at: null,
        hard_deadline_at: null,
        next_attempt_at: future,
        parked_reason: null,
        updated_at: past,
      },
    ],
    active_runs_total: 12,
  };
}

async function connect(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: 'Connect your manager' }).click();
  await page.getByLabel('Manager API token').fill('activity-token');
  await page.getByRole('button', { name: 'Connect workspace', exact: true }).click();
  await expect(page.getByText('Loaded task stays visible')).toBeVisible();
  await page
    .getByRole('navigation', { name: 'Main navigation' })
    .getByRole('button', { name: 'Activity' })
    .click();
  await expect(page.getByRole('heading', { name: 'What Werft is doing.' })).toBeVisible();
}

test('shows authenticated backend activity, global stages, workers, and event inspection', async ({
  page,
}) => {
  const requests: { path: string; authorization: string | undefined }[] = [];
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    requests.push({ path, authorization: request.headers().authorization });
    if (request.headers().authorization !== 'Bearer activity-token')
      return route.fulfill({ status: 401, json: { detail: 'unauthorized' } });
    if (path.endsWith('/activity')) return route.fulfill({ json: snapshot() });
    if (path.endsWith('/quota')) return route.fulfill({ json: { accounts: [] } });
    if (path.endsWith('/projects')) return route.fulfill({ json: [] });
    if (path.endsWith('/system')) return route.fulfill({ json: { containers: [] } });
    if (path.endsWith('/runs/event-only-run')) {
      const eventDetail = detail(
        'event-only-run',
        'Inspect a run absent from the loaded list',
        'failed',
      );
      eventDetail.events = [
        {
          id: requests.filter((entry) => entry.path.endsWith('/runs/event-only-run')).length,
          event_type:
            requests.filter((entry) => entry.path.endsWith('/runs/event-only-run')).length > 1
              ? 'live_refresh'
              : 'initial_state',
          payload: {},
          created_at: now(),
        },
      ];
      return route.fulfill({ json: eventDetail });
    }
    if (path.endsWith('/runs/loaded-run'))
      return route.fulfill({ json: detail('loaded-run', 'Loaded task stays visible', 'running') });
    return route.fulfill({
      json: { runs: [summary('loaded-run', 'Loaded task stays visible', 'running')], total: 1 },
    });
  });

  await connect(page);
  const monitor = page.getByRole('region', { name: 'Backend activity' });
  await expect(monitor.getByRole('button', { name: /Queue\s*5/ })).toBeVisible();
  await expect(monitor.getByRole('button', { name: /Working\s*3/ })).toBeVisible();
  await expect(monitor.getByText('12', { exact: true }).first()).toBeVisible();
  await expect(
    monitor.getByText('Showing 3 of 12 open tasks. Stage counts cover the entire workspace.'),
  ).toBeVisible();
  await monitor.getByRole('button', { name: /Queue\s*5/ }).click();
  await expect(monitor.getByText('Only one queued task is loaded')).toBeVisible();
  await expect(monitor.getByText('Loaded task stays visible')).toHaveCount(0);
  await monitor.getByRole('button', { name: /Show all current tasks/ }).click();
  await expect(monitor.getByText('lease renewal')).toBeVisible();
  await expect(monitor.getByText('Manager is attending this agent session')).toBeVisible();
  await expect(monitor.getByText('Next check in', { exact: false }).first()).toBeVisible();
  await expect(monitor.getByText('Retry pending')).toBeVisible();
  await expect(monitor.getByText('Last error', { exact: false })).toBeVisible();

  await monitor.getByRole('button', { name: /Failed Inspect a run absent/ }).click();
  const inspector = page.getByRole('dialog', { name: 'Run details' });
  await expect(
    inspector.getByRole('heading', { name: 'Inspect a run absent from the loaded list' }),
  ).toBeVisible();
  // The inspector independently polls its selected run after opening.
  await expect(inspector.getByText('live refresh')).toBeVisible();
  // Opening fetches once for selection and once for detail. A third request
  // proves the inspector continues polling after those initial requests.
  await expect
    .poll(
      () => requests.filter((request) => request.path.endsWith('/runs/event-only-run')).length,
      { timeout: 8000 },
    )
    .toBeGreaterThanOrEqual(3);
  expect(requests.some((request) => request.path.endsWith('/runs/event-only-run'))).toBe(true);
  expect(requests.length).toBeGreaterThan(4);
  expect(requests.every((request) => request.authorization === 'Bearer activity-token')).toBe(true);
});

test('preserves the last activity snapshot through a transient failure, then recovers', async ({
  page,
}) => {
  let activityCalls = 0;
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().headers().authorization !== 'Bearer activity-token')
      return route.fulfill({ status: 401, json: {} });
    if (path.endsWith('/activity')) {
      activityCalls++;
      if (activityCalls === 2)
        return route.fulfill({ status: 503, json: { detail: 'temporary outage' } });
      return route.fulfill({ json: snapshot(activityCalls >= 3 ? 2 : 1) });
    }
    if (path.endsWith('/quota')) return route.fulfill({ json: { accounts: [] } });
    if (path.endsWith('/projects')) return route.fulfill({ json: [] });
    if (path.endsWith('/system')) return route.fulfill({ json: { containers: [] } });
    return route.fulfill({ json: { runs: [], total: 0 } });
  });

  await connect(page);
  const monitor = page.getByRole('region', { name: 'Backend activity' });
  await page.getByRole('button', { name: 'Refresh activity' }).click();
  await expect(monitor.getByRole('status')).toContainText(
    'Backend activity could not be refreshed',
  );
  await expect(monitor.getByRole('heading', { name: 'Live updates interrupted.' })).toBeVisible();
  await expect(monitor.getByText('Loaded task stays visible')).toBeVisible();
  await monitor.getByRole('button', { name: 'Retry' }).click();
  await expect(monitor.getByText('Snapshot refreshed live')).toBeVisible();
  await expect(monitor.getByRole('status')).toHaveCount(0);
  await expect(
    monitor.getByRole('heading', { name: 'One agent session is active.' }),
  ).toBeVisible();
});

test('asks to reconnect when activity polling receives an expired credential', async ({ page }) => {
  let activityCalls = 0;
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().headers().authorization !== 'Bearer activity-token')
      return route.fulfill({ status: 401, json: {} });
    if (path.endsWith('/activity')) {
      activityCalls++;
      return route.fulfill(activityCalls === 1 ? { json: snapshot() } : { status: 401, json: {} });
    }
    if (path.endsWith('/quota')) return route.fulfill({ json: { accounts: [] } });
    if (path.endsWith('/projects')) return route.fulfill({ json: [] });
    if (path.endsWith('/system')) return route.fulfill({ json: { containers: [] } });
    return route.fulfill({ json: { runs: [], total: 0 } });
  });

  await connect(page);
  await page.getByRole('button', { name: 'Refresh activity' }).click();
  await expect(page.getByRole('dialog', { name: 'Connect your workspace' })).toBeVisible();
  await expect(
    page.getByRole('dialog').getByText('Your connection has expired. Enter a valid manager token.'),
  ).toBeVisible();
  await expect(
    page.getByRole('region', { name: 'Backend activity' }).getByRole('status'),
  ).toContainText('Connection expired. Reconnect to resume live activity.');
  await expect(page.evaluate(() => localStorage.getItem('werft_token'))).resolves.toBeNull();
});
