import { test, expect } from '@playwright/test';

for (const viewport of [
  { width: 1600, height: 1000 },
  { width: 390, height: 844 },
]) {
  test(`project canvas drills into agents and keeps context at ${viewport.width}px`, async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.setViewportSize(viewport);
    await page.goto('/');
    await expect(page.locator('.project-tile')).toHaveCount(3);
    await page.screenshot({ path: `../.impeccable/review/canvas-projects-${viewport.width}.png` });
    await page.locator('.project-tile').filter({ hasText: 'atlas-web' }).click();
    await expect(page.locator('.project-hub')).toContainText('atlas-web');
    const agent = page.getByRole('button', { name: 'Open task Build the new analytics workspace' });
    await expect(agent).toBeVisible();
    await expect(page.locator('.project-tile')).toHaveCount(0);
    await agent.click();
    const panel = page.getByRole('complementary', { name: 'Agent details' });
    await expect(panel).toBeVisible();
    await expect(panel.getByRole('region', { name: 'Task progress' })).toBeVisible();
    await expect(panel.locator('[aria-current="step"]')).toHaveText('Working');
    await expect(page.locator('.project-hub')).toBeVisible();
    await expect(page.locator('.inspector-dialog')).not.toBeVisible();
    await page.screenshot({ path: `../.impeccable/review/canvas-agent-${viewport.width}.png` });
    await panel.getByRole('button', { name: 'Session', exact: true }).click();
    await expect(panel.locator('.session pre')).toContainText('Sample session output');
    await panel.getByRole('button', { name: 'Close run details' }).click();
    await expect(panel).toHaveCount(0);
    await expect(agent).toBeFocused();
    await agent.click();
    await page.keyboard.press('Escape');
    await expect(panel).toHaveCount(0);
    await expect(agent).toBeFocused();
    await page.getByRole('button', { name: 'All projects', exact: true }).click();
    await expect(page.locator('.project-tile')).toHaveCount(3);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true,
    );
    expect(errors).toEqual([]);
  });
}
