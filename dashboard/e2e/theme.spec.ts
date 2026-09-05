import { expect, test } from '@playwright/test';

test('theme choice persists across navigation and reload', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/');
  await page.getByRole('button', { name: 'Switch to dark mode' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(async () => {
    const contrast = await page
      .locator('.nav-item')
      .filter({ hasText: 'Agents' })
      .evaluate((element) => {
        const channels = (color: string) => (color.match(/[\d.]+/g) ?? []).slice(0, 3).map(Number);
        const luminance = (color: string) =>
          channels(color).reduce((sum, value, index) => {
            const n = value / 255;
            return (
              sum +
              (n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4) *
                [0.2126, 0.7152, 0.0722][index]
            );
          }, 0);
        let surface: Element | null = element;
        while (surface && getComputedStyle(surface).backgroundColor === 'rgba(0, 0, 0, 0)')
          surface = surface.parentElement;
        const foreground = luminance(getComputedStyle(element).color);
        const background = luminance(
          surface ? getComputedStyle(surface).backgroundColor : 'rgb(255, 255, 255)',
        );
        return (
          (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05)
        );
      });
    expect(contrast).toBeGreaterThanOrEqual(4.5);
  }).toPass();
  await page.getByRole('button', { name: 'Talk to Werft', exact: true }).click();
  await expect(page.locator('.conversation-composer textarea')).toBeVisible();
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.getByRole('button', { name: 'Switch to light mode' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
});
