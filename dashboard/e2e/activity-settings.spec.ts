import { test, expect } from '@playwright/test';

test('activity views isolate information and Settings remains readable at both sizes', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await page
    .getByRole('navigation', { name: 'Main navigation' })
    .getByRole('button', { name: 'Activity', exact: true })
    .click();
  const monitor = page.getByRole('region', { name: 'Backend activity' });
  const views = monitor.getByRole('navigation', { name: 'Activity views' });
  await expect(monitor.locator('.session-row')).toHaveCount(5);
  await expect(monitor.locator('.activity-event-list')).toHaveCount(0);
  await expect(monitor.locator('.worker')).toHaveCount(0);
  await page.screenshot({ path: '../.impeccable/review/activity-tasks.png', fullPage: true });
  await views.getByRole('button', { name: /Events/ }).click();
  await expect(monitor.locator('.session-row')).toHaveCount(0);
  await expect(monitor.locator('.activity-event-list li')).toHaveCount(6);
  await monitor.getByRole('button', { name: 'Next events', exact: true }).click();
  await expect(monitor.locator('.activity-event-list li')).toHaveCount(1);
  await monitor.getByRole('button', { name: 'Previous events', exact: true }).click();
  await page.screenshot({ path: '../.impeccable/review/activity-events.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: '../.impeccable/review/activity-events-mobile.png',
    fullPage: true,
  });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await views.getByRole('button', { name: 'Backend', exact: true }).click();
  await expect(monitor.locator('.worker')).toHaveCount(3);
  await expect(monitor.locator('.activity-event-list')).toHaveCount(0);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  for (const [width, height, name] of [
    [1440, 900, 'settings-desktop'],
    [390, 844, 'settings-mobile'],
  ] as const) {
    await page.setViewportSize({ width, height });
    const fontSizes = await page
      .locator(
        '.settings-row strong, .settings-row p, .settings-row > .status, .settings-row > .muted, .settings-row > kbd',
      )
      .evaluateAll((elements) => elements.map((el) => parseFloat(getComputedStyle(el).fontSize)));
    expect(fontSizes.length).toBeGreaterThan(5);
    expect(Math.min(...fontSizes)).toBeGreaterThanOrEqual(14);
    expect(
      await page
        .locator('.settings-row .button')
        .first()
        .evaluate((el) => el.getBoundingClientRect().height),
    ).toBeGreaterThanOrEqual(44);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true,
    );
    await page.screenshot({ path: `../.impeccable/review/${name}.png`, fullPage: true });
  }
});
