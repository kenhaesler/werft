import { expect, test, type Page } from '@playwright/test';

const now = () => new Date().toISOString();

const run = {
  id: 'run-agent',
  project_slug: 'platform',
  issue_number: 42,
  issue_title: 'Make the conversation panel reliable',
  status: 'running',
  attempt_count: 1,
  max_attempts: 3,
  latest_outcome: null,
  parked_reason: null,
  pr_number: null,
  pr_url: null,
  created_at: now(),
  updated_at: now(),
};

function runDetail() {
  return {
    ...run,
    branch_name: 'codex/conversation',
    base_sha: null,
    merge_commit_sha: null,
    error_message: null,
    result: null,
    events: [],
    attempts: [],
    artifacts: [],
  };
}

async function mockManager(page: Page, unavailable = false) {
  let agentFailed = false;
  await page.addInitScript(() => localStorage.setItem('werft_token', 'conversation-token'));
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (request.headers().authorization !== 'Bearer conversation-token')
      return route.fulfill({ status: 401, json: { detail: 'unauthorized' } });
    if (path.endsWith('/conversations/orchestrator')) {
      if (unavailable)
        return route.fulfill({
          json: {
            messages: [],
            available: false,
            unavailable_reason:
              'The orchestrator is not available right now. Try again after it reconnects.',
          },
        });
      return route.fulfill({
        json: {
          messages: [
            {
              id: 'welcome',
              role: 'assistant',
              content: 'What would you like to clarify?',
              status: 'answered',
              created_at: now(),
            },
          ],
          available: true,
        },
      });
    }
    if (path.endsWith('/conversations/orchestrator/messages')) {
      const content = (request.postDataJSON() as { content: string }).content;
      return route.fulfill({
        json: {
          available: true,
          messages: [
            { id: 'question', role: 'user', content, status: 'queued', created_at: now() },
            {
              id: 'reply',
              role: 'assistant',
              content: 'I will keep the existing review gate in place.',
              status: 'answered',
              created_at: now(),
            },
          ],
        },
      });
    }
    if (path.endsWith('/conversations/run-agent'))
      return route.fulfill({
        json: {
          available: true,
          messages: agentFailed
            ? [
                {
                  id: 'failed',
                  role: 'user',
                  content: 'Do not merge yet.',
                  status: 'failed',
                  error: 'Manager rejected the message before delivery.',
                  created_at: now(),
                },
              ]
            : [
                {
                  id: 'agent',
                  role: 'assistant',
                  content: 'The task agent is waiting for direction.',
                  status: 'answered',
                  created_at: now(),
                },
              ],
        },
      });
    if (path.endsWith('/conversations/run-agent/messages')) {
      agentFailed = true;
      return route.fulfill({
        json: {
          available: true,
          messages: [
            {
              id: 'failed',
              role: 'user',
              content: 'Do not merge yet.',
              status: 'failed',
              error: 'Manager rejected the message before delivery.',
              created_at: now(),
            },
          ],
        },
      });
    }
    if (path.endsWith('/runs/run-agent/artifacts'))
      return route.fulfill({ json: { artifacts: [] } });
    if (path.endsWith('/runs/run-agent')) return route.fulfill({ json: runDetail() });
    if (path.endsWith('/runs')) return route.fulfill({ json: { runs: [run], total: 1 } });
    if (path.endsWith('/projects')) return route.fulfill({ json: [] });
    if (path.endsWith('/quota')) return route.fulfill({ json: { accounts: [] } });
    if (path.endsWith('/system')) return route.fulfill({ json: { containers: [] } });
    if (path.endsWith('/activity'))
      return route.fulfill({
        json: {
          generated_at: now(),
          manager: {
            available: true,
            started_at: now(),
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
    return route.fulfill({ json: { runs: [], total: 0 } });
  });
}

for (const viewport of [
  { width: 1440, height: 1000, label: 'desktop' },
  { width: 390, height: 844, label: 'mobile' },
]) {
  test(`talk to Werft sends a scoped message at ${viewport.label}`, async ({ page }) => {
    await mockManager(page);
    await page.setViewportSize(viewport);
    await page.goto('/');
    if (viewport.width < 600) await page.getByRole('button', { name: 'Open navigation' }).click();
    await page.getByRole('button', { name: 'Talk to Werft', exact: true }).click();
    const conversation = page.getByRole('region', { name: 'Talk to Werft' });
    await expect(conversation.getByText('What would you like to clarify?')).toBeVisible();
    await conversation
      .getByLabel('Message orchestrator')
      .fill('Should the review gate remain enabled?');
    await conversation.getByRole('button', { name: 'Send message' }).click();
    await expect(
      conversation.getByText('I will keep the existing review gate in place.'),
    ).toBeVisible();
    await expect(conversation.getByText('queued', { exact: true })).toBeVisible();
    await expect(
      page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    ).resolves.toBe(true);
    await page.screenshot({
      path: `../.impeccable/review/conversation-${viewport.label}.png`,
      fullPage: false,
    });
  });
}

test('agent conversation has a distinct scope, exposes failed delivery, and closes cleanly', async ({
  page,
}) => {
  await mockManager(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/');
  await page.getByRole('button', { name: 'Agents', exact: true }).click();
  await page.getByText('Make the conversation panel reliable', { exact: true }).click();
  const inspector = page.getByRole('dialog', { name: 'Run details' });
  await inspector.getByRole('button', { name: 'Conversation', exact: true }).click();
  const conversation = inspector.getByRole('region', { name: 'Task conversation' });
  await expect(conversation.getByText('The task agent is waiting for direction.')).toBeVisible();
  await conversation.getByLabel('Message this task’s agent').fill('Do not merge yet.');
  await conversation.getByRole('button', { name: 'Send message' }).click();
  await expect(conversation.getByText(/^failed\b/)).toBeVisible();
  await expect(
    conversation.getByText('Manager rejected the message before delivery.'),
  ).toBeVisible();
  await expect(
    conversation.getByText('I will keep the existing review gate in place.'),
  ).toHaveCount(0);
  await inspector.getByRole('button', { name: 'Close run details' }).click();
  await expect(inspector).not.toBeVisible();
});

test('shows the manager-provided conversation unavailability reason', async ({ page }) => {
  await mockManager(page, true);
  await page.goto('/');
  await page.getByRole('button', { name: 'Talk to Werft', exact: true }).click();
  const conversation = page.getByRole('region', { name: 'Talk to Werft' });
  await expect(
    conversation.getByText(
      'The orchestrator is not available right now. Try again after it reconnects.',
    ),
  ).toBeVisible();
  await expect(conversation.getByRole('button', { name: 'Send message' })).toBeDisabled();
});
