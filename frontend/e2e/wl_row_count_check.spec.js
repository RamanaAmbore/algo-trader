// Diagnostic: print the actual row count per Winners/Losers tab.
import { test, expect } from '@playwright/test';

const USER = 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test('count rows per Winners/Losers tab', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"], input#username, input#s-user', USER);
  await page.fill('input[name="password"], input#password, input#s-pass', PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);

  const tabs = ['Underlying', 'Midcap', 'Smallcap', 'Holdings', 'Positions'];
  for (const card of ['.wl-tile-win', '.wl-tile-loss']) {
    for (const t of tabs) {
      const tab = page.locator(`${card} .wl-tab:has-text("${t}")`);
      if (await tab.count() === 0) continue;
      await tab.click();
      await page.waitForTimeout(150);
      const rows = await page.locator(`${card} .wl-rows .wl-row`).count();
      console.log(`${card} :: ${t} = ${rows} rows`);
    }
  }

  // Compute max-height + scrollHeight on Holdings tab
  await page.locator('.wl-tile-win .wl-tab:has-text("Holdings")').click();
  await page.waitForTimeout(200);
  const dims = await page.locator('.wl-tile-win .wl-rows').evaluate((el) => ({
    clientHeight: el.clientHeight,
    scrollHeight: el.scrollHeight,
    maxHeight: getComputedStyle(el).maxHeight,
    overflowY: getComputedStyle(el).overflowY,
    children: el.children.length,
  }));
  console.log('[win.holdings .wl-rows dims]', dims);
});
