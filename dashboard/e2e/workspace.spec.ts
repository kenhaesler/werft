import { test, expect } from '@playwright/test';

test('drills from phase to task to evidence without showing runtime metadata upfront', async ({
  page,
}) => {
  await page.goto('/');
  const monitor = page.getByRole('region', { name: 'Backend activity' });
  await expect(page.getByText('Demo workspace', { exact: true })).toBeVisible();
  await expect(
    page.getByText('Showing sample tasks and machines. Connect a manager to view live activity.'),
  ).toBeVisible();
  expect(
    await page
      .locator('.preview-description')
      .evaluate((el) => parseFloat(getComputedStyle(el).fontSize)),
  ).toBeGreaterThanOrEqual(14);
  await expect(page.locator('.session-row')).toHaveCount(1);
  await expect(page.locator('.worker-list')).not.toBeVisible();
  await monitor.getByRole('button', { name: /Review\s*1/ }).click();
  const row = monitor.locator('.session-row');
  await expect(row).toHaveCount(1);
  await expect(row.locator('.session-runtime')).not.toBeVisible();
  await row.locator('summary').click();
  await expect(row.getByText('Waiting for your review decision')).toBeVisible();
  await row.getByRole('button', { name: 'Open task' }).click();
  const inspector = page.getByRole('dialog', { name: 'Run details' });
  await inspector.getByRole('button', { name: /Evidence/ }).click();
  await expect(inspector.getByText('transcript.jsonl', { exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await monitor.locator('.backend-summary').click();
  await expect(monitor.locator('.worker-list')).toBeVisible();
});

test('preview navigation, search, evidence, review, and task creation', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await expect(page.getByText('Demo workspace')).toBeVisible();
  await page.getByRole('button', { name: /Review work/ }).click();
  const inspector = page.getByRole('dialog', { name: 'Run details' });
  await expect(
    inspector.getByRole('heading', { name: 'Create accessible command menu primitives' }),
  ).toBeVisible();
  await inspector.getByRole('button', { name: /Evidence/ }).click();
  const download = page.waitForEvent('download');
  await inspector.getByRole('button', { name: 'Download transcript.jsonl' }).click();
  expect((await download).suggestedFilename()).toContain('sample-');
  await inspector.getByRole('button', { name: 'Accept work' }).click();
  await expect(inspector.getByText('Merging', { exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.keyboard.press('Control+k');
  await expect(page.getByRole('textbox', { name: 'Search commands and tasks' })).toBeFocused();
  await page.getByRole('textbox', { name: 'Search commands and tasks' }).fill('Virtual');
  await page.getByRole('dialog').getByRole('button', { name: 'Virtual machine' }).click();
  await expect(page.getByRole('heading', { name: 'Virtual machine' })).toBeVisible();
  await page.getByRole('button', { name: 'Inspect', exact: true }).first().click();
  await expect(
    inspector.getByRole('heading', { name: 'Build the new analytics workspace' }),
  ).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Agents', exact: true }).click();
  await page.getByRole('textbox', { name: 'Search tasks', exact: true }).fill('timeout');
  await expect(page.locator('.run-item')).toHaveCount(1);
  await page.getByRole('button', { name: 'New task', exact: true }).click();
  await page.getByLabel('What should the agent do?').fill('Build a searchable audit trail');
  await page.getByRole('button', { name: 'Queue preview task' }).click();
  await expect(
    page.locator('.run-item').filter({ hasText: 'Build a searchable audit trail' }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test('live connection, authenticated actions, conflict recovery, and disconnect', async ({
  page,
}) => {
  let status = 'awaiting_review';
  let conflict = true;
  const requests: { url: string; auth: string | undefined }[] = [];
  const run = () => ({
    id: 'live-1',
    project_slug: 'live-project',
    issue_number: 9,
    issue_title: 'Live review task',
    status,
    attempt_count: 1,
    max_attempts: 3,
    latest_outcome: 'success',
    parked_reason: null,
    pr_number: null,
    pr_url: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  });
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    requests.push({ url: request.url(), auth: request.headers().authorization });
    if (request.headers().authorization !== 'Bearer live-token')
      return route.fulfill({ status: 401, json: { detail: 'unauthorized' } });
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/quota')) return route.fulfill({ json: { accounts: [] } });
    if (path.endsWith('/projects'))
      return route.fulfill({
        json: [
          { id: 'p1', slug: 'live-project', owner: 'owner', repo: 'repo', lifecycle: 'bootstrap' },
        ],
      });
    if (path.endsWith('/activity'))
      return route.fulfill({
        json: {
          generated_at: new Date().toISOString(),
          manager: {
            available: true,
            started_at: new Date().toISOString(),
            workers: {},
            recent_operations: [],
            live_driver_run_ids: [],
          },
          status_counts: { awaiting_review: 1 },
          recent_events: [],
          active_runs: [],
          active_runs_total: 0,
        },
      });
    if (path.endsWith('/system'))
      return route.fulfill({ status: 503, json: { detail: 'unavailable' } });
    if (path.endsWith('/review/accept')) {
      if (conflict) {
        conflict = false;
        return route.fulfill({ status: 409, json: {} });
      }
      status = 'merging';
      return route.fulfill({ json: run() });
    }
    if (path.endsWith('/runs/live-1'))
      return route.fulfill({
        json: {
          ...run(),
          branch_name: 'test-branch',
          events: [],
          attempts: [],
          artifacts: [],
          result: null,
          error_message: null,
        },
      });
    return route.fulfill({ json: { runs: [run()], total: 1 } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Connect manager' }).click();
  await page.getByLabel('Manager API token').fill('bad-token');
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Connect manager', exact: true })
    .click();
  await expect(page.getByRole('alert')).toContainText('did not accept');
  expect(await page.evaluate(() => localStorage.getItem('werft_token'))).toBeNull();
  await page.getByLabel('Manager API token').fill('live-token');
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Connect manager', exact: true })
    .click();
  await expect(page.getByText('Demo workspace')).toHaveCount(0);
  await expect(page.getByText('Machine unavailable', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: /Review work/ }).click();
  await page.getByRole('button', { name: 'Accept work' }).click();
  await expect(page.getByRole('status')).toContainText('changed state');
  await page.getByRole('button', { name: 'Accept work' }).click();
  await expect(page.getByRole('dialog').getByText('Merging', { exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'Disconnect', exact: true }).click();
  expect(await page.evaluate(() => localStorage.getItem('werft_token'))).toBeNull();
  expect(requests.every((request) => !request.url.includes('token'))).toBe(true);
});

test('desktop and mobile visual evidence, keyboard focus and overflow', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  expect(
    await page.locator('.activity-monitor').evaluate((el) => el.getBoundingClientRect().width),
  ).toBeGreaterThan(1000);
  expect(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight)).toBe(
    true,
  );
  await page.screenshot({
    path: '../.impeccable/review/desktop.png',
    fullPage: true,
    animations: 'disabled',
  });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThan(1250);
  expect(
    await page
      .locator('.work-pipeline button')
      .first()
      .evaluate((el) => el.getBoundingClientRect().width),
  ).toBeGreaterThan(85);
  await page.screenshot({
    path: '../.impeccable/review/mobile.png',
    fullPage: true,
    animations: 'disabled',
  });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await expect(page.locator('.sidebar .nav-item.active')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: 'Open navigation' })).toBeFocused();
  await expect(page.locator('.sidebar')).toHaveAttribute('inert', '');
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await page.getByRole('button', { name: 'Projects', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Projects' })).toBeVisible();
  await page.getByRole('button', { name: 'Add project' }).click();
  await page.getByLabel('GitHub repository').fill('sample/new-project');
  await page.getByLabel('Project name', { exact: true }).fill('new-project');
  await page.getByRole('button', { name: 'Add to preview' }).click();
  await expect(page.getByRole('heading', { name: 'new-project', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
