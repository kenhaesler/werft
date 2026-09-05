import { test, expect } from '@playwright/test';
import { demoProjects, demoRuns } from '../src/lib/demo';

test('links every concurrent task and reflects an updated backend state', async ({ page }) => {
  const tasks = Array.from({ length: 7 }, (_, index) => ({
    ...demoRuns[0],
    id: `parallel-${index}`,
    project_slug: 'atlas-web',
    issue_title: `Parallel task ${index + 1}`,
    issue_number: index + 1,
    status: 'running',
  }));
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.addInitScript(() => localStorage.setItem('werft_token', 'canvas-test'));
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/projects')) return route.fulfill({ json: demoProjects });
    if (path.endsWith('/runs'))
      return route.fulfill({ json: { runs: tasks, total: tasks.length } });
    if (path.endsWith('/quota')) return route.fulfill({ json: { accounts: [] } });
    return route.fulfill({ status: 503, json: { detail: 'Unavailable in this test' } });
  });
  await page.goto('/');
  await page.locator('.project-tile').filter({ hasText: 'atlas-web' }).click();
  await expect(page.locator('.workstream')).toHaveCount(7);
  await expect(page.locator('.workbench .link-map path')).toHaveCount(7);
  const boxes = await page.locator('.workstream').evaluateAll((nodes) =>
    nodes.map((node) => {
      const r = node.getBoundingClientRect();
      return { x: r.x, y: r.y, right: r.right, bottom: r.bottom };
    }),
  );
  for (let i = 0; i < boxes.length; i++)
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i],
        b = boxes[j];
      expect(a.right <= b.x || b.right <= a.x || a.bottom <= b.y || b.bottom <= a.y).toBe(true);
    }
  tasks[0].status = 'awaiting_review';
  await page.getByRole('button', { name: 'Refresh projects' }).click();
  await expect(page.locator('[data-run-id="parallel-0"]')).toContainText('Ready for review');
  await expect(page.locator('[data-run-id="parallel-1"]')).toContainText('Pending result');
});

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
