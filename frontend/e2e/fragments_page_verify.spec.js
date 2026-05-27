// Verify the /agents/fragments page renders, the workspace tab strip
// includes it, and the three seeded notify + three seeded condition
// fragments appear with the correct SYSTEM pills.

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

let _cachedToken = null;
async function login(page) {
  if (!_cachedToken) {
    for (const u of ['ambore', 'rambo']) {
      const r = await page.request.post(`${BASE}/api/auth/login`, {
        data: { username: u, password: _PASS },
      });
      if (r.ok()) { _cachedToken = (await r.json()).access_token; break; }
    }
    if (!_cachedToken) throw new Error(`login failed against ${BASE}`);
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, _cachedToken);
}

test.describe('fragments page', () => {
  test.use({ viewport: { width: 1366, height: 768 } });

  test(`tab strip carries Fragments + page renders all seeds [${BASE}]`, async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/agents/fragments`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.aw-tabs', { state: 'visible', timeout: 15_000 });

    // Fragments tab is in the strip and lit as active.
    const fragTab = page.locator('.aw-tab:text-is("Fragments")');
    await expect(fragTab).toBeVisible();
    await expect(fragTab).toHaveClass(/aw-tab-active/);

    // Page title shows.
    await expect(page.locator('h1.page-title-chip')).toHaveText(/Fragments/);

    // Wait for the seed list to land. Three notify + three condition.
    await page.waitForSelector('.frag-row', { state: 'visible', timeout: 15_000 });
    const allNames = await page.locator('.frag-name').allTextContents();
    for (const name of [
      'notify-critical-trio',
      'notify-log-only',
      'notify-telegram-only',
      'loss-positions-acct-default',
      'loss-positions-total-default',
      'near-market-close-30m',
    ]) {
      expect(allNames, `seeded fragment ${name} should appear`).toContain(name);
    }

    // Each system row carries the SYSTEM pill.
    const systemPills = page.locator('.frag-pill-system');
    expect(await systemPills.count()).toBeGreaterThanOrEqual(6);
  });

  test(`filter chip narrows to notify only [${BASE}]`, async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/agents/fragments`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.frag-row', { state: 'visible', timeout: 15_000 });

    await page.locator('.filter-btn:text-is("notify")').click();
    // After filtering, condition group header must NOT be visible.
    await expect(page.locator('h2.grp-title:text-is("CONDITION")')).toHaveCount(0);
    await expect(page.locator('h2.grp-title:text-is("NOTIFY")')).toBeVisible();
  });

  test(`expanding a row reveals the body JSON [${BASE}]`, async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/agents/fragments`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.frag-row', { state: 'visible', timeout: 15_000 });

    // Click the critical-trio row header.
    await page.locator('.frag-head:has-text("notify-critical-trio")').click();

    // Body pre is now visible AND contains "telegram".
    const body = page.locator('.frag-row-open .frag-body-pre');
    await expect(body).toBeVisible();
    await expect(body).toContainText('telegram');
    await expect(body).toContainText('email');
    await expect(body).toContainText('log');
  });
});
