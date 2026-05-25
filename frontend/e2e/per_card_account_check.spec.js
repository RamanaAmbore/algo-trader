// Confirm each AccountMultiSelect on /dashboard is independent —
// flipping one card's picker doesn't cascade into the others.
import { test, expect } from '@playwright/test';

const USER = 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test('Per-card account state is independent (Equity / Winners / Losers)', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"], input#username, input#s-user', USER);
  await page.fill('input[name="password"], input#password, input#s-pass', PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
  await page.evaluate(() => {
    for (const k of Object.keys(sessionStorage)) {
      if (k.startsWith('dash.')) sessionStorage.removeItem(k);
    }
  });
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);

  // Pick the first account (ZG0790 typically) in the Equity card only
  const eqPicker = page.locator('.bucket-eq .rbq-multi-trigger');
  await eqPicker.click();
  await page.waitForTimeout(150);
  const firstOpt = page.locator('.rbq-multi-panel .rbq-multi-option').first();
  await firstOpt.click();
  await page.keyboard.press('Escape');
  await page.waitForTimeout(150);

  // sessionStorage: only dash.eqAccounts should be populated
  const stored = await page.evaluate(() => ({
    eq:  sessionStorage.getItem('dash.eqAccounts'),
    win: sessionStorage.getItem('dash.winAccounts'),
    los: sessionStorage.getItem('dash.losAccounts'),
  }));
  console.log('[after Equity pick]', stored);
  expect(JSON.parse(stored.eq).length).toBe(1);
  expect(JSON.parse(stored.win || '[]').length).toBe(0);
  expect(JSON.parse(stored.los || '[]').length).toBe(0);

  // Visible label confirms each card's picker reflects its own state
  const eqLabel = (await page.locator('.bucket-eq .rbq-multi-trigger').textContent()).trim();
  const winLabel = (await page.locator('.wl-tile-win .rbq-multi-trigger').textContent()).trim();
  console.log(`equity="${eqLabel}" winners="${winLabel}"`);
  // Equity label changes (shows "ZG0790 ×" or similar — single
  // account selected); Winners stays on "All accounts" placeholder.
  expect(eqLabel).not.toMatch(/All accounts/);
  expect(winLabel).toMatch(/All accounts/);

  await page.screenshot({ path: 'test-results/per-card-account.png', fullPage: false });
});
