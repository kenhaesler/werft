import { test, expect } from '@playwright/test';

test('evidence is grouped and previewed as inert text', async ({ page }) => {
  await page.goto('/');
  const row = page.locator('.session-row').first();
  await row.locator('summary').click();
  await row.getByRole('button', { name: 'Open task' }).click();
  const inspector = page.getByRole('dialog', { name: 'Run details' });
  await inspector.getByRole('button', { name: /Evidence/ }).click();
  await expect(inspector.getByRole('heading', { name: 'Changes' })).toBeVisible();
  await expect(inspector.getByRole('heading', { name: 'Logs and output' })).toBeVisible();
  await inspector.getByRole('button', { name: 'Preview changes.diff' }).click();
  await expect(
    inspector.getByText(
      'This sample preview is text only; collected HTML is never executed in the dashboard.',
    ),
  ).toBeVisible();
});
