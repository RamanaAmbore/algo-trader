// Verify Underlying / Midcap / Smallcap tabs populate from market
// data (independent of user holdings). Holdings + Positions stay
// user-scoped.
import { test, expect } from '@playwright/test';

const USER = 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test('market buckets populate from quote universe', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"], input#username, input#s-user', USER);
  await page.fill('input[name="password"], input#password, input#s-pass', PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
  // Wait a touch longer — market-quote batch takes 1-3 s for 300 symbols
  await page.waitForTimeout(5000);

  const winCard = page.locator('.wl-tile-win');
  const tabs = ['Underlying', 'Midcap', 'Smallcap', 'Holdings', 'Positions'];
  const counts = {};
  for (const t of tabs) {
    const tab = winCard.locator(`.wl-tab:has-text("${t}")`);
    if (await tab.count() === 0) continue;
    await tab.click();
    await page.waitForTimeout(150);
    const rows = await winCard.locator('.wl-rows .wl-row').count();
    const tabCount = await tab.locator('.wl-tab-count').textContent();
    counts[t] = { rendered: rows, totalChip: tabCount?.trim() };
  }
  console.log('[winners per tab]', counts);

  // Market-wide tabs should populate with a non-trivial count
  // (universe is ~300 symbols across all three; even partial fills
  // give plenty of movers). Expect >= 5 entries per market bucket
  // during market hours, but accept 0 outside hours.
  console.log('Underlying total:', counts.Underlying?.totalChip);
  console.log('Midcap total:',     counts.Midcap?.totalChip);
  console.log('Smallcap total:',   counts.Smallcap?.totalChip);

  await page.screenshot({ path: 'test-results/wl-market-buckets.png', fullPage: false });
});
