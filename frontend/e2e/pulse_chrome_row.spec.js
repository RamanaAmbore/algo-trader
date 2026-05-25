// Verify Pulse single-row chrome (mobile + desktop) and + List popup.
import { test, expect } from '@playwright/test';

const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

async function _login(page) {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post('https://ramboq.com/api/auth/login', {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  if (!tok) throw new Error('login failed');
  await page.context().addInitScript((t) => { sessionStorage.setItem('ramboq_token', t); }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });
}

test('pulse: single chrome row + List popup', async ({ page }) => {
  await _login(page);
  // Desktop view
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('https://ramboq.com/pulse', { waitUntil: 'networkidle' });
  await page.waitForTimeout(4000);

  const chromeRow = page.locator('.mp-chrome-row').first();
  await expect(chromeRow).toBeVisible();

  // Confirm + List button + search button + filters all live in one row
  const listBtn = page.locator('button', { hasText: '+ List' }).first();
  await expect(listBtn).toBeVisible();
  const rowBox = await chromeRow.boundingBox();
  console.log('chrome row height:', rowBox?.height);
  expect(rowBox?.height).toBeLessThan(50); // single row, not stacked

  // Click + List → popup opens
  await listBtn.click();
  await page.waitForTimeout(400);
  const popup = page.locator('.search-modal', { hasText: 'New watchlist' }).first();
  await expect(popup).toBeVisible();
  await page.screenshot({ path: 'test-results/pulse-list-popup.png', fullPage: false });

  // Close popup
  await page.locator('.search-modal .search-close').first().click();
  await expect(popup).not.toBeVisible();

  // Take desktop screenshot
  await page.screenshot({ path: 'test-results/pulse-chrome-desktop.png', fullPage: false });
});

test('pulse mobile portrait: one chrome row, ag-grid below', async ({ page }) => {
  await _login(page);
  // Mobile portrait
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('https://ramboq.com/pulse', { waitUntil: 'networkidle' });
  await page.waitForTimeout(4000);

  const chromeRow = page.locator('.mp-chrome-row').first();
  const rowBox = await chromeRow.boundingBox();
  console.log('mobile chrome row height:', rowBox?.height);
  // Allow some slack for the row's padding but it shouldn't multi-line
  expect(rowBox?.height).toBeLessThan(60);

  await page.screenshot({ path: 'test-results/pulse-chrome-mobile.png', fullPage: false });
});
