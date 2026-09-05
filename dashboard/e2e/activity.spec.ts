import { expect, test, type Page } from '@playwright/test';

const now = () => new Date().toISOString();

test('keeps many parallel sessions readable and exposes runtime details on demand', async ({
  page,
}) => {
  const data = snapshot();
  data.active_runs = Array.from({ length: 24 }, (_, i) => ({
    ...data.active_runs[0],
    run_id: `parallel-${i}`,
    issue_number: 100 + i,
    issue_title:
      i === 0
        ? 'Implement organization permissions across the analytics workspace, background exports, and shared dashboards'
        : `Parallel task ${i + 1}`,
    project_slug: i % 2 ? 'data-pipeline' : 'analytics-workspace',
    container_id: `environment-${i}`,
    status: 'running',
  }));
  data.active_runs_total = 24;
  data.manager.live_driver_run_ids = data.active_runs.map((run) => run.run_id);
  data.status_counts = {
    queued: 0,
    running: 24,
    awaiting_ci: 0,
    awaiting_review: 0,
    merging: 0,
    failed: 0,
    parked: 0,
    merged: 9,
  };
  await page.addInitScript(() => localStorage.setItem('werft_token', 'activity-token'));
  await page.route('**/api/v1/**', (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/activity')) return route.fulfill({ json: data });
    if (
      path.endsWith('/runs') &&
      new URL(route.request().url()).searchParams.get('status') === 'awaiting_review'
    )
      return route.fulfill({ json: { runs: [], total: 0 } });
    if (path.endsWith('/quota')) return route.fulfill({ json: { accounts: [] } });
    if (path.endsWith('/projects')) return route.fulfill({ json: [] });
    if (path.endsWith('/system')) return route.fulfill({ status: 503, json: {} });
    return route.fulfill({ json: { runs: [], total: 0 } });
  });
  await page.setViewportSize({ width: 1784, height: 1214 });
  await page.goto('/');
  await page.getByRole('button', { name: 'Activity', exact: true }).click();
  await expect(page.getByRole('heading', { name: '24 active sessions' })).toBeVisible();
  await expect(page.locator('.session-row')).toHaveCount(6);
  await expect(page.locator('.session-runtime').first()).not.toBeVisible();
  await page.locator('.session-row summary').first().focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('.session-runtime').first()).toBeVisible();
  await expect(page.locator('.session-runtime').first()).toContainText('environment-0');
  await page.keyboard.press('Enter');
  await page.screenshot({
    path: '../.impeccable/review/parallel-sessions-desktop.png',
    fullPage: true,
  });
  await expect(page.locator('.session-row')).toHaveCount(6);
  for (let i = 0; i < 3; i++) await page.getByRole('button', { name: 'Next tasks' }).click();
  await expect(page.getByText('19–24 of 24 loaded tasks')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Next tasks' })).toBeDisabled();
  await expect(page.getByText('Parallel task 24', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Previous tasks' }).click();
  await expect(page.getByText('13–18 of 24 loaded tasks')).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('.session-row summary').first().click();
  await expect(page.locator('.session-runtime').first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({
    path: '../.impeccable/review/parallel-sessions-mobile.png',
    fullPage: false,
  });
});

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
  await page.getByRole('button', { name: 'Connect manager' }).click();
  await page.getByLabel('Manager API token').fill('activity-token');
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Connect manager', exact: true })
    .click();
  await page
    .getByRole('navigation', { name: 'Main navigation' })
    .getByRole('button', { name: 'Activity' })
    .click();
  await expect(page.getByText('Loaded task stays visible', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Activity' })).toBeVisible();
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
    if (path.endsWith('/events')) {
      const query = new URL(request.url()).searchParams.get('q') ?? '';
      if (query === 'no matching event') return route.fulfill({ json: { total: 0, events: [] } });
      if (query === 'audit')
        return route.fulfill({
          json: {
            total: 1,
            events: [
              {
                id: 77,
                run_id: 'event-only-run',
                project_slug: 'platform',
                issue_number: 44,
                issue_title: 'Inspect a run absent from the loaded list',
                run_status: 'failed',
                event_type: 'audit_recorded',
                phase: 'review',
                from_status: null,
                to_status: null,
                created_at: now(),
                payload: { reason: 'audit' },
              },
            ],
          },
        });
      return route.fulfill({ json: { total: 0, events: [] } });
    }
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
  await monitor
    .locator('.session-row')
    .filter({ hasText: 'Loaded task stays visible' })
    .locator('summary')
    .click();
  await expect(monitor.getByText('Manager is attending this agent session')).toBeVisible();
  await monitor.getByRole('button', { name: /Backend.*error/ }).click();
  await expect(monitor.getByText('lease renewal')).toBeVisible();
  await expect(monitor.locator('.session-row')).toHaveCount(0);
  await expect(
    monitor.locator('.worker summary').getByText('Next check in', { exact: false }).first(),
  ).toBeVisible();
  await expect(monitor.getByText('Retry pending')).toBeVisible();
  await monitor
    .locator('.worker')
    .filter({ hasText: 'CI & merge checks' })
    .locator('summary')
    .click();
  await expect(monitor.getByText('Last error', { exact: false })).toBeVisible();

  await monitor.getByRole('button', { name: /Events/ }).click();
  await expect(monitor.locator('.worker')).toHaveCount(0);
  const eventSearch = monitor.getByRole('textbox', { name: 'Search event history' });
  await eventSearch.fill('no matching event');
  await monitor.getByRole('button', { name: 'Search', exact: true }).click();
  await expect(monitor.getByText('No recorded events match this search.')).toBeVisible();
  await eventSearch.fill('audit');
  await monitor.getByRole('button', { name: 'Search', exact: true }).click();
  const matchingEvent = monitor
    .locator('.event-history-list li')
    .filter({ hasText: 'Inspect a run absent from the loaded list' });
  await matchingEvent.locator('summary').click();
  await matchingEvent.getByRole('button', { name: 'Open task' }).click();
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
  await expect(monitor.getByRole('heading', { name: '1 active session' })).toBeVisible();
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
  await expect(page.getByRole('dialog', { name: 'Connect manager' })).toBeVisible();
  await expect(
    page.getByRole('dialog').getByText('Your connection has expired. Enter a valid manager token.'),
  ).toBeVisible();
  await expect(
    page.getByRole('region', { name: 'Backend activity' }).getByRole('status'),
  ).toContainText('Connection expired. Reconnect to resume live activity.');
  await expect(page.evaluate(() => localStorage.getItem('werft_token'))).resolves.toBeNull();
});
