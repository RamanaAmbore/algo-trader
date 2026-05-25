// Confirm collapse → expand keeps the grid populated (regression
// guard for the bind:this orphaning bug).
import { test, expect } from '@playwright/test';

const USER = 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test('Capital ag-Grid survives collapse + expand', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"], input#username, input#s-user', USER);
  await page.fill('input[name="password"], input#password, input#s-pass', PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });

  // Reset collapse state so Capital starts expanded
  await page.evaluate(() => {
    for (const k of Object.keys(localStorage)) {
      if (k.startsWith('ramboq.collapse.')) localStorage.removeItem(k);
    }
  });
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);

  const cap = page.locator('.bucket-cap');
  // Count rows before collapse — Funds + Margin together
  const before = await cap.locator('.ag-row').count();
  console.log('[before collapse] ag rows in Capital:', before);
  expect(before).toBeGreaterThan(0);

  // Collapse
  await cap.locator('.collapse-btn').first().click();
  await page.waitForTimeout(250);
  await expect(cap).toHaveClass(/is-collapsed/);

  // Expand again
  await cap.locator('.collapse-btn').first().click();
  await page.waitForTimeout(400);
  await expect(cap).not.toHaveClass(/is-collapsed/);

  // Grids should still be populated — REGRESSION GUARD
  const after = await cap.locator('.ag-row').count();
  console.log('[after expand] ag rows in Capital:', after);
  expect(after).toBe(before);
});

test('Top Winners ag-Grid survives collapse + expand', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"], input#username, input#s-user', USER);
  await page.fill('input[name="password"], input#password, input#s-pass', PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
  await page.evaluate(() => {
    for (const k of Object.keys(localStorage)) {
      if (k.startsWith('ramboq.collapse.')) localStorage.removeItem(k);
    }
  });
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);

  const win = page.locator('.wl-tile-win');
  const before = await win.locator('.ag-row').count();
  console.log('[before] win rows:', before);
  expect(before).toBeGreaterThan(0);

  await win.locator('.collapse-btn').first().click();
  await page.waitForTimeout(250);
  await win.locator('.collapse-btn').first().click();
  await page.waitForTimeout(400);

  const after = await win.locator('.ag-row').count();
  console.log('[after] win rows:', after);
  expect(after).toBe(before);
});
