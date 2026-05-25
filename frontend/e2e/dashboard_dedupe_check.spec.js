// One-shot verification that the Winners/Losers buckets dedupe a
// symbol that's held in multiple accounts (the user reported GMDCLTD
// appearing twice in the Smallcap winners tab).
import { test, expect } from '@playwright/test';

const USER = 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test('Smallcap winners tab has no duplicate symbols', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="username"], input#username, input#s-user').first().fill(USER);
  await page.locator('input[name="password"], input#password, input#s-pass').first().fill(PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);

  const winCard = page.locator('.wl-tile-win');
  // Click the Smallcap tab (3rd: Underlying, Midcap, Smallcap, …)
  await winCard.locator('.wl-tab:has-text("Smallcap")').click();

  const syms = await winCard.locator('.wl-row .wl-sym').allTextContents();
  console.log('[smallcap winners]', syms);

  const unique = new Set(syms);
  expect(unique.size).toBe(syms.length);
});
