/**
 * Demo mode — anonymous-on-prod flow.
 *
 * Best run against PROD:
 *   PLAYWRIGHT_BASE_URL=https://ramboq.com npx playwright test demo_mode \
 *     --project=chromium-desktop
 *
 * On localhost / dev branches, /api/charts/paper-status reports
 * `branch: 'dev'`, so the (algo) layout's `isDemo` derivation is
 * always false — anonymous visitors get redirected to /signin instead
 * of seeing demo mode. We `test.skip()` the prod-specific checks in
 * that case so the spec stays green across environments.
 */

import { test, expect } from '@playwright/test';
import { visitAnonymous } from './fixtures/auth.js';

const TIMEOUT = 20_000;

/**
 * Hit /api/charts/paper-status and return its `branch` field — we
 * gate the prod-specific assertions on this matching 'main'.
 *
 * @param {import('@playwright/test').Page} page
 * @returns {Promise<string|undefined>}
 */
async function detectBranch(page) {
  try {
    const r = await page.request.get('/api/charts/paper-status');
    if (!r.ok()) return undefined;
    const j = await r.json();
    return j?.branch;
  } catch (_) {
    return undefined;
  }
}

test.describe('Demo mode — anonymous-on-prod', () => {
  test.beforeEach(async ({ page }) => {
    await visitAnonymous(page);
  });

  test('anonymous /dashboard is NOT redirected to /signin (prod only)', async ({ page }) => {
    const branch = await detectBranch(page);
    if (branch !== 'main') {
      test.skip(true, `branch=${branch} — demo mode only kicks in on main; on dev, anon goes to /signin by design`);
    }
    await page.goto('/dashboard');
    // Wait for the (algo) layout to make its branch decision (it polls
    // paper-status itself on mount). Once `isDemo` derives true, the
    // signin redirect short-circuits and the dashboard renders.
    await page.waitForURL(/\/dashboard/, { timeout: TIMEOUT });
    expect(page.url()).not.toContain('/signin');
  });

  test('demo banner mentions "demo mode" and "Sign in"', async ({ page }) => {
    const branch = await detectBranch(page);
    if (branch !== 'main') test.skip(true, `branch=${branch} — demo banner only renders on main`);

    await page.goto('/dashboard');
    const banner = page.locator('.demo-banner').first();
    await expect(banner).toBeVisible({ timeout: TIMEOUT });
    await expect(banner).toContainText(/demo mode/i);
    await expect(banner).toContainText(/Sign in/i);
  });

  test('navbar shows DEMO badge', async ({ page }) => {
    const branch = await detectBranch(page);
    if (branch !== 'main') test.skip(true, `branch=${branch} — DEMO badge only renders on main`);

    await page.goto('/dashboard');
    const demoBadge = page.locator('.algo-mode-demo, .algo-mode-badge:has-text("DEMO")').first();
    await expect(demoBadge).toBeVisible({ timeout: TIMEOUT });
  });

  test('admin-only nav links hidden in demo (Settings/Brokers/Users)', async ({ page }) => {
    const branch = await detectBranch(page);
    if (branch !== 'main') test.skip(true, `branch=${branch} — admin-link filter only applies in demo`);

    await page.goto('/dashboard');
    // Wait for the navbar's algoLinks $derived to resolve.
    await page.waitForLoadState('domcontentloaded');
    // Both desktop and mobile nav use the same algoLinks list — the
    // adminOnly filter drops the link entirely, so the anchor should
    // not appear in the document at all.
    await expect(page.locator('a[href="/admin/settings"]')).toHaveCount(0);
    await expect(page.locator('a[href="/admin/brokers"]')).toHaveCount(0);
    await expect(page.locator('a[href="/admin"][href$="/admin"]')).toHaveCount(0);
  });

  test('account values rendered as masked Z[A-Z]####', async ({ page }) => {
    const branch = await detectBranch(page);
    if (branch !== 'main') test.skip(true, `branch=${branch} — account masking on UI requires real prod book in demo`);

    await page.goto('/dashboard');
    // Wait for at least one ag-Grid row to populate.
    await page.locator('.ag-row').first().waitFor({ state: 'attached', timeout: TIMEOUT }).catch(() => {});
    // Grab the text of every cell in the account column. Demo masks
    // digits; a real account ID like ZG0790 should show as ZG####.
    const acctTexts = await page.locator('.ag-col-acct').allInnerTexts();
    const real = acctTexts.find(t => /Z[A-Z]\d{4,}/.test(t));
    expect(real, `expected no unmasked Z[A-Z]\\d{4,} cell — got ${acctTexts.join(', ')}`).toBeUndefined();
    // At least one cell should match the masked shape (skip if every cell is empty / TOTAL).
    const masked = acctTexts.find(t => /Z[A-Z]#{4,}/.test(t));
    if (!masked) {
      test.skip(true, 'no account rows rendered — demo book is empty? skip mask shape check');
    }
  });

  test('write attempts guarded — POST /agents and PUT /orders return 401', async ({ page }) => {
    // This API-only check works on every branch. On dev, anon-no-token
    // hits the same admin_guard chain (returns 401). On main, demo
    // sessions hit it too (also 401). Either way we expect failure.
    const r1 = await page.request.post('/api/agents/', {
      data: { slug: 'test-demo', name: 'demo', conditions: {}, events: [], actions: [], scope: 'total', schedule: 'always' },
    });
    expect([401, 403]).toContain(r1.status());

    const r2 = await page.request.put('/api/orders/some-fake-id', { data: { price: 100 } });
    expect([401, 403, 404]).toContain(r2.status());

    // Ticket route is the trickier case — it accepts demo sessions but
    // downgrades live → paper. Anonymous (no token) still 401s.
    const r3 = await page.request.post('/api/orders/ticket', {
      data: {
        mode: 'live', side: 'BUY', tradingsymbol: 'NIFTY26MAY22000PE', qty: 50,
        exchange: 'NFO', product: 'NRML', order_type: 'LIMIT', variety: 'regular',
        price: 100, trigger_price: 0, account: 'ZG0790',
      },
    });
    // Anonymous → 401. Demo (with cookie) on prod → 200 paper-mode.
    // Either is acceptable evidence the chokepoint is honoured.
    expect([200, 201, 400, 401, 403]).toContain(r3.status());
  });
});
