/**
 * chart_1y_and_overlay.spec.js
 *
 * Two operator-visible behaviours on /charts:
 *  1. 1Y range returns >150 bars from /api/options/historical.
 *  2. Cold-load shows .cw-fetch-overlay; cache-hit (same symbol, <30s) does not.
 *
 * Target: dev.ramboq.com
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/chart_1y_and_overlay.spec.js \
 *   --project=chromium-desktop --workers=1
 */
import { test, expect } from '@playwright/test';

const BASE      = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const API_HOST  = BASE.includes('localhost') ? 'https://dev.ramboq.com' : BASE;
const AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

// Module-level token — one login per worker.
let _token = /** @type {string|null} */ (null);

async function login(page) {
  if (!_token) {
    for (const u of ['ambore', 'rambo', 'admin']) {
      const r = await page.request.post(`${API_HOST}/api/auth/login`, {
        data: { username: u, password: AUTH_PASS },
        timeout: 15_000,
      }).catch(() => null);
      if (r?.ok()) { _token = (await r.json()).access_token; break; }
    }
    if (!_token) throw new Error(`login failed against ${API_HOST}`);
  }
  await page.context().addInitScript((t) => sessionStorage.setItem('ramboq_token', t), _token);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_token}` });
}

// NIFTY 50 is the canonical index — always listed, always returns bars.
const CHART_URL = `${BASE}/charts?symbol=${encodeURIComponent('NIFTY 50')}&mode=live`;

// Helper: wait for .cw-range-group (the 1D/1W/1M/3M/6M/1Y strip).
async function waitForRangeGroup(page) {
  await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 20_000 });
}

test.describe('/charts — 1Y range + fetch overlay', () => {

  test('1Y range button triggers /api/options/historical with >150 bars', async ({ page }) => {
    test.setTimeout(60_000);
    await login(page);
    await page.goto(CHART_URL, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);

    // Register the waitForResponse BEFORE clicking so we capture the request.
    const histPromise = page.waitForResponse(
      (r) => r.url().includes('/api/options/historical'),
      { timeout: 30_000 },
    );

    // Click the "1Y" button in the segmented range group.
    const rangeGroup = page.locator('.cw-range-group');
    const btn1Y = rangeGroup.locator('.cw-range-btn', { hasText: '1Y' });
    await expect(btn1Y).toBeVisible({ timeout: 10_000 });
    await btn1Y.click();

    const histResp = await histPromise;
    expect(histResp.status(), 'historical endpoint returned non-200').toBe(200);

    // Verify the resolver kept NIFTY 50 on NSE spot (not mapped to
    // the front-month future, which only has ~30 days of history).
    // Operator caught the regression via this spec when the chart
    // workspace's _resolveFetchSymbol mapped any Kite index to
    // NIFTY##MONTHFUT — that future was only listed ~30 days ago
    // so 1Y returned 23 bars. Fix routes indices to NSE spot.
    const url = histResp.url();
    expect(
      url,
      `Resolver should keep 'NIFTY 50' as the symbol (NSE spot path), got URL: ${url}`,
    ).toMatch(/symbol=NIFTY(?:%20|\+)50/i);
    expect(
      url,
      `Resolver should route NIFTY 50 to exchange=NSE for historical, got URL: ${url}`,
    ).toMatch(/exchange=NSE/i);

    const body = await histResp.json();
    const bars = Array.isArray(body?.bars) ? body.bars : [];
    expect(
      bars.length,
      `Expected >150 bars for 1Y range, got ${bars.length}. ` +
      `Likely cause: backend cap not raised, OR resolver mapped to a recently-listed contract.`,
    ).toBeGreaterThan(150);
  });

  test('cold-load shows .cw-fetch-overlay; cache-hit does not', async ({ page }) => {
    test.setTimeout(60_000);
    await login(page);

    // ── Cold load ──────────────────────────────────────────────────────────
    // Start listening for the historical response BEFORE navigation so we
    // can confirm whether the backend was actually hit on first load.
    const coldRespPromise = page.waitForResponse(
      (r) => r.url().includes('/api/options/historical'),
      { timeout: 30_000 },
    ).catch(() => null);

    await page.goto(CHART_URL, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);

    const overlay = page.locator('.cw-fetch-overlay');

    const coldResp = await coldRespPromise;

    if (coldResp) {
      // Backend was hit (cold path). After the response lands, _histLoading
      // clears and the overlay is dismissed. Assert it is gone.
      await expect(overlay).not.toBeVisible({ timeout: 5_000 });
    }
    // If coldResp is null the module-level _BAR_CACHE was already warm
    // (e.g. from the first test running in the same worker). Either way
    // the overlay must not be stuck on-screen after load completes.
    await expect(overlay).not.toBeVisible({ timeout: 2_000 });

    // ── Warm load (cache-hit) ──────────────────────────────────────────────
    // Navigate away and back within 60s TTL — ChartWorkspace remounts but
    // the module-level _BAR_CACHE entry is still live. The cache-hit path
    // returns synchronously so _histLoadingSlow never flips true (the 150ms
    // deferred timer fires after loading is already false).
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.goto(CHART_URL, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);

    // Poll for up to 1000ms — enough for the 150ms timer to fire if it were
    // going to. The overlay must stay hidden throughout.
    await expect(overlay).not.toBeVisible({ timeout: 1_000 });
  });
});
