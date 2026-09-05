import { test, expect, type Page, type Route } from '@playwright/test';

type Run = {
  id: string;
  project_slug: string;
  issue_number: number;
  issue_title: string;
  status: string;
  attempt_count: number;
  max_attempts: number;
  latest_outcome: string | null;
  parked_reason: string | null;
  pr_number: number | null;
  pr_url: string | null;
  created_at: string;
  updated_at: string;
};

const makeRun = (id: string, title: string, status = 'running', project = 'alpha'): Run => ({
  id,
  project_slug: project,
  issue_number: Number(id.replace(/\D/g, '')) || 1,
  issue_title: title,
  status,
  attempt_count: 1,
  max_attempts: 3,
  latest_outcome: null,
  parked_reason: null,
  pr_number: null,
  pr_url: null,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
});

const runs = [
  ...Array.from({ length: 205 }, (_, index) => makeRun(`run-${index}`, `Routine task ${index}`)),
  makeRun('old-review', 'Old review task', 'awaiting_review', 'beta'),
  ...Array.from({ length: 16 }, (_, index) => makeRun(`page-${index}`, `Page task ${index}`)),
];

async function mockManager(page: Page, requests: string[]) {
  let projectLifecycle = 'bootstrap';
  await page.route('**/api/v1/**', async (route: Route) => {
    const url = new URL(route.request().url());
    requests.push(`${url.pathname}${url.search}`);
    if (route.request().headers().authorization !== 'Bearer query-token')
      return route.fulfill({ status: 401, json: { detail: 'unauthorized' } });
    if (url.pathname.endsWith('/quota')) return route.fulfill({ json: { accounts: [] } });
    if (url.pathname.endsWith('/projects'))
      return route.fulfill({
        json: [
          {
            id: 'alpha-id',
            slug: 'alpha',
            owner: 'owner',
            repo: 'alpha',
            lifecycle: 'bootstrap',
            onboarded_at: null,
            created_at: new Date().toISOString(),
          },
          {
            id: 'beta-id',
            slug: 'beta',
            owner: 'owner',
            repo: 'beta',
            lifecycle: 'oracle_gated',
            onboarded_at: null,
            created_at: new Date().toISOString(),
          },
        ],
      });
    if (url.pathname.endsWith('/flip')) {
      projectLifecycle = url.searchParams.get('to') ?? projectLifecycle;
      const to = JSON.parse(route.request().postData() ?? '{}').to as string;
      projectLifecycle = to;
      return route.fulfill({
        json: {
          id: 'alpha-id',
          slug: 'alpha',
          owner: 'owner',
          repo: 'alpha',
          lifecycle: projectLifecycle,
          onboarded_at: null,
          created_at: new Date().toISOString(),
        },
      });
    }
    if (url.pathname.endsWith('/system'))
      return route.fulfill({ status: 503, json: { detail: 'offline' } });
    if (url.pathname.endsWith('/activity'))
      return route.fulfill({
        json: {
          generated_at: new Date().toISOString(),
          manager: {
            available: true,
            started_at: null,
            workers: {},
            recent_operations: [],
            live_driver_run_ids: [],
          },
          status_counts: {},
          recent_events: [],
          active_runs: [],
          active_runs_total: 0,
        },
      });
    if (url.pathname.endsWith('/runs')) {
      if (url.searchParams.get('status') === 'awaiting_review')
        return route.fulfill({
          json: { runs: [runs.find((run) => run.id === 'old-review')], total: 1 },
        });
      if (url.searchParams.has('q')) {
        const q = url.searchParams.get('q') ?? '';
        if (q === 'slow') await new Promise((resolve) => setTimeout(resolve, 350));
        const result =
          q === '%'
            ? [makeRun('literal-percent', 'Literal % task')]
            : [
                makeRun(
                  `match-${q}`,
                  `${q} result`,
                  'running',
                  url.searchParams.get('project') ?? 'alpha',
                ),
              ];
        return route.fulfill({ json: { runs: result, total: result.length } });
      }
      const offset = Number(url.searchParams.get('offset') ?? 0);
      const limit = Number(url.searchParams.get('limit') ?? 200);
      const filtered = url.searchParams.getAll('statuses').length
        ? runs.filter((run) => url.searchParams.getAll('statuses').includes(run.status))
        : runs;
      return route.fulfill({
        json: { runs: filtered.slice(offset, offset + limit), total: filtered.length },
      });
    }
    return route.fulfill({ json: {} });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Connect manager' }).click();
  await page.getByLabel('Manager API token').fill('query-token');
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Connect manager', exact: true })
    .click();
  await expect(page.getByText('Demo workspace')).toHaveCount(0);
}

test('uses server query coverage for review, filters, pagination and command search', async ({
  page,
}) => {
  const requests: string[] = [];
  await mockManager(page, requests);
  await page
    .locator('nav[aria-label="Main navigation"]')
    .getByRole('button', { name: /^Review/ })
    .click();
  await expect(page.getByText('Old review task')).toBeVisible();
  expect(requests.some((request) => request.includes('/runs?status=awaiting_review&limit=1'))).toBe(
    true,
  );

  await page.getByRole('button', { name: 'Agents', exact: true }).click();
  await page.getByRole('button', { name: 'Needs attention', exact: true }).click();
  await expect(page.locator('.run-item')).toHaveCount(1);
  expect(
    requests.some(
      (request) =>
        request.includes('statuses=awaiting_review') && request.includes('statuses=parked'),
    ),
  ).toBe(true);
  await page.getByRole('combobox', { name: 'Filter by project' }).selectOption('beta');
  await page.getByRole('textbox', { name: 'Search tasks' }).fill('%');
  await expect(page.getByText('Literal % task')).toBeVisible();
  expect(
    requests.some((request) => request.includes('project=beta') && request.includes('q=%25')),
  ).toBe(true);

  await page.getByRole('button', { name: 'All tasks', exact: true }).click();
  await page.getByRole('combobox', { name: 'Filter by project' }).selectOption('all');
  await page.getByRole('textbox', { name: 'Search tasks' }).fill('');
  await page.getByRole('button', { name: 'Next tasks' }).click();
  await expect(page.getByText('Routine task 8')).toBeVisible();
  await page.getByRole('button', { name: 'Previous tasks' }).click();
  await expect(page.getByText('Routine task 0')).toBeVisible();
  await page.keyboard.press('Control+k');
  await page.getByRole('textbox', { name: 'Search commands and tasks' }).fill('Page');
  await expect(page.getByRole('dialog').getByText('Page result')).toBeVisible();
  expect(requests.some((request) => request.includes('/runs?q=Page&limit=5'))).toBe(true);
});

test('aborts stale task queries so an older response cannot replace the newer search', async ({
  page,
}) => {
  const requests: string[] = [];
  await mockManager(page, requests);
  await page.getByRole('button', { name: 'Agents', exact: true }).click();
  const search = page.getByRole('textbox', { name: 'Search tasks' });
  await search.fill('slow');
  await page.waitForTimeout(230);
  await search.fill('fresh');
  await expect(page.getByText('fresh result')).toBeVisible();
  await page.waitForTimeout(400);
  await expect(page.getByText('slow result')).toHaveCount(0);
  expect(requests.some((request) => request.includes('q=slow'))).toBe(true);
  expect(requests.some((request) => request.includes('q=fresh'))).toBe(true);
});

test('flips lifecycle through the mounted project settings controls', async ({ page }) => {
  const requests: string[] = [];
  await mockManager(page, requests);
  await page
    .locator('nav[aria-label="Main navigation"]')
    .getByRole('button', { name: 'Projects' })
    .click();
  const entry = page.locator('.project-entry').filter({ hasText: 'alpha' }).first();
  await entry.getByText('Project settings').click();
  await entry.getByRole('button', { name: 'Set oracle-gated' }).click();
  await expect(entry.getByText('CI checked')).toBeVisible();
  expect(requests.some((request) => request.includes('/projects/alpha-id/flip'))).toBe(true);
});
