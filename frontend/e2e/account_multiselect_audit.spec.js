// Verifies every position/holdings surface has the account MultiSelect:
//   /pulse           — MarketPulse toolbar
//   /performance     — PerformancePage toolbar
//   /dashboard       — Equity card header
//   /admin/options   — already had it before this sprint
import { test, expect } from '@playwright/test';

const USER = 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

async function signIn(page) {
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="username"], input#username, input#s-user').first().fill(USER);
  await page.locator('input[name="password"], input#password, input#s-pass').first().fill(PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
  for (let i = 0; i < 10; i++) {
    const has = await page.evaluate(() => !!sessionStorage.getItem('ramboq_token'));
    if (has) break;
    await new Promise((r) => setTimeout(r, 300));
  }
}

test('every positions/holdings surface has account MultiSelect', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);

  // /dashboard Equity card
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  const dashAcct = page.locator('.bucket-eq .rbq-multi-trigger');
  await expect(dashAcct).toBeVisible();
  console.log('[/dashboard] MultiSelect placeholder:', await dashAcct.textContent());

  // /pulse — MarketPulse toolbar, picker mounts inside .ml-auto cluster
  await page.goto('/pulse', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);
  const pulseAcct = page.locator('.rbq-multi-trigger[aria-label="Account filter"]');
  await expect(pulseAcct).toBeVisible();
  console.log('[/pulse] MultiSelect placeholder:', await pulseAcct.textContent());

  // /performance — PerformancePage toolbar
  await page.goto('/performance', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);
  const perfAcct = page.locator('.acct-multi .rbq-multi-trigger').first();
  await expect(perfAcct).toBeVisible();
  console.log('[/performance] MultiSelect placeholder:', await perfAcct.textContent());

  // /admin/options — already had it
  await page.goto('/admin/options', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  const optsAcct = page.locator('.rbq-multi-trigger').first();
  await expect(optsAcct).toBeVisible();
});
