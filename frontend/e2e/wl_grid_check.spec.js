// Verify W/L cards render via ag-Grid (not button list) and that
// the rows populate with Symbol / LTP / Δ % columns.
import { test, expect } from '@playwright/test';

const USER = 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test('Winners + Losers render ag-Grid rows with Symbol/LTP/%', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"], input#username, input#s-user', USER);
  await page.fill('input[name="password"], input#password, input#s-pass', PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(4000);

  // ag-Grid wrappers present
  await expect(page.locator('.wl-tile-win .dash-wl-grid')).toBeVisible();
  await expect(page.locator('.wl-tile-loss .dash-wl-grid')).toBeVisible();

  // Headers: Symbol · LTP · Δ %
  const winHeaders = await page.locator('.wl-tile-win .ag-header-cell-label').allTextContents();
  console.log('[winners headers]', winHeaders.map(s => s.trim()));
  expect(winHeaders.some(h => h.trim() === 'Symbol')).toBe(true);
  expect(winHeaders.some(h => h.trim() === 'LTP')).toBe(true);
  expect(winHeaders.some(h => h.includes('%'))).toBe(true);

  // Holdings tab default — row count
  const winRowCount = await page.locator('.wl-tile-win .ag-row').count();
  console.log('[winners] visible ag rows:', winRowCount);
  expect(winRowCount).toBeGreaterThan(0);

  // Switch to Underlying (market tab) — multiselect should disable
  await page.locator('.wl-tile-win .wl-tab:has-text("Underlying")').click();
  await page.waitForTimeout(300);
  const winPickerDisabled = await page.locator('.wl-tile-win .rbq-multi-trigger')
    .evaluate((el) => el.getAttribute('disabled') !== null);
  console.log('[winners underlying tab] picker disabled:', winPickerDisabled);
  expect(winPickerDisabled).toBe(true);

  // Switch back to Holdings — picker should re-enable
  await page.locator('.wl-tile-win .wl-tab:has-text("Holdings")').click();
  await page.waitForTimeout(200);
  const winPickerEnabled = await page.locator('.wl-tile-win .rbq-multi-trigger')
    .evaluate((el) => el.getAttribute('disabled') === null);
  console.log('[winners holdings tab] picker enabled:', winPickerEnabled);
  expect(winPickerEnabled).toBe(true);

  await page.screenshot({ path: 'test-results/wl-grid-view.png', fullPage: false });
});

test('Capital card: Margin + Funds ag-Grids render', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"], input#username, input#s-user', USER);
  await page.fill('input[name="password"], input#password, input#s-pass', PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(3000);

  const minis = page.locator('.bucket-cap .dash-mini-grid');
  await expect(minis.nth(0)).toBeVisible();
  await expect(minis.nth(1)).toBeVisible();

  // Funds grid has a pinned-bottom TOTAL row
  const totalRow = page.locator('.bucket-cap .ag-row-pinned');
  await expect(totalRow.first()).toBeVisible();
});
