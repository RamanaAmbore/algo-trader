// Confirm a collapsed card actually shrinks to its header height
// (not stretched by the grid parent's align-items: stretch).
import { test, expect } from '@playwright/test';

const USER = 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test('Collapsed Capital card shrinks vs expanded Equity sibling', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"], input#username, input#s-user', USER);
  await page.fill('input[name="password"], input#password, input#s-pass', PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });

  // Start from a clean state — wipe any previously stored collapses.
  await page.evaluate(() => {
    for (const k of Object.keys(localStorage)) {
      if (k.startsWith('ramboq.collapse.')) localStorage.removeItem(k);
    }
  });
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);

  const cap = page.locator('.bucket-cap');
  const eq  = page.locator('.bucket-eq');
  const expBefore = (await eq.boundingBox()).height;
  const capBefore = (await cap.boundingBox()).height;
  console.log(`[before] capital=${capBefore}px equity=${expBefore}px`);
  // Both should be equal-height before collapse (grid stretch).
  expect(Math.abs(capBefore - expBefore)).toBeLessThan(20);

  // Collapse Capital
  await cap.locator('.collapse-btn').first().click();
  await page.waitForTimeout(300);
  const capAfter = (await cap.boundingBox()).height;
  const expAfter = (await eq.boundingBox()).height;
  console.log(`[after collapse] capital=${capAfter}px equity=${expAfter}px`);

  // Capital should be drastically smaller (< 80 px — just the header)
  expect(capAfter).toBeLessThan(80);
  // Equity stays roughly the same height
  expect(Math.abs(expAfter - expBefore)).toBeLessThan(50);

  await page.screenshot({ path: 'test-results/collapse-shrink.png', fullPage: false });
});
