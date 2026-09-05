import { test, expect } from '@playwright/test';

test('task inspector explains the agent and keeps technical evidence behind drilldowns', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/');
  const row = page.locator('.session-row').first();
  await row.locator('summary').click();
  await row.getByRole('button', { name: 'Open task' }).click();
  const panel = page.getByRole('dialog', { name: 'Run details' });
  await expect(panel.getByRole('heading', { name: 'Agent session in progress' })).toBeVisible();
  await expect(panel.getByRole('heading', { name: 'Claude agent' })).toBeVisible();
  await expect(panel.getByText('Manager is monitoring this session')).toBeVisible();
  await expect(panel.getByText('Next step', { exact: true })).toBeVisible();
  await expect(panel.locator('.task-technical dl')).not.toBeVisible();
  for (const tab of ['Session', 'Timeline', 'Evidence', 'Attempts', 'Overview']) {
    const control = panel.getByRole('button', { name: new RegExp(`^${tab}`) });
    await control.click();
    await expect(control).toHaveAttribute('aria-pressed', 'true');
  }
  await page.screenshot({ path: '../.impeccable/review/inspector-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await panel.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
  await page.screenshot({ path: '../.impeccable/review/inspector-mobile.png' });
  await panel.getByText('Technical details', { exact: true }).click();
  await expect(panel.getByText('preview-environment', { exact: true })).toBeVisible();
  await panel.getByRole('button', { name: 'View timeline' }).click();
  await expect(panel.locator('.timeline-event').first()).toContainText('Status changed to working');
  await expect(panel.locator('.event-payload pre').first()).not.toBeVisible();
  await panel.locator('.event-payload summary').first().click();
  await expect(panel.locator('.event-payload pre').first()).toContainText('running');
  await panel.getByRole('button', { name: /Evidence/ }).click();
  await expect(panel.getByText('transcript.jsonl', { exact: true })).toBeVisible();
});
