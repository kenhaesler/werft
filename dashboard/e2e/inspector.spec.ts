import { test, expect } from '@playwright/test';

for (const viewport of [
  { width: 1440, height: 1000 },
  { width: 390, height: 844 },
]) {
  test(`closes an active Session with the close button and Escape at ${viewport.width}px`, async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.setViewportSize(viewport);
    await page.goto('/');
    const row = page.locator('.session-row').first();
    await row.locator('summary').click();
    const opener = row.getByRole('button', { name: 'Open task' });
    const panel = page.getByRole('dialog', { name: 'Run details' });

    for (const method of ['button', 'escape']) {
      await opener.click();
      await panel.getByRole('button', { name: 'Session', exact: true }).click();
      const output = panel.locator('.session pre');
      await expect(output).toContainText('Sample session output');
      expect((await output.innerText()).match(/Sample session output/g)).toHaveLength(1);
      if (method === 'button') {
        await panel.getByRole('button', { name: 'Close run details' }).click();
      } else {
        await page.keyboard.press('Escape');
      }
      await expect(panel).not.toBeVisible();
      await expect(page.locator('.inspector-dialog .session')).toHaveCount(0);
      await expect(opener).toBeFocused();
    }
    expect(errors).toEqual([]);
  });
}

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
