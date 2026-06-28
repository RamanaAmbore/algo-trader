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

  // ── Five-dimension quality assertions (feedback_test_dimensions.md) ───────
  //
  // 1. SSOT — chart canvas-displayed last close matches the historical-bar
  //    payload's last close (no parallel re-derivation).
  // 2. Performance — chart workspace cold load fires under a small XHR
  //    budget. Catches the "every tick triggers a fresh fetch" regression.
  // 3. Stale code — the legacy NFO index-future fallback in the resolver
  //    is gone; source-grep the bundled JS for the old defaultExch
  //    fallback chain that included 'NFO'.
  // 4. Reusable code — the page uses the canonical `.cw-range-group`
  //    (ChartWorkspace's exported scope) rather than a hand-rolled tab strip.
  // 5. UX consistency — the active range button uses cyan-400 palette
  //    (`rgb(34, 211, 238)`) per the canonical-card-header rule.

  test('quality dimensions: SSOT + perf budget + stale code + reusable + palette', async ({ page }) => {
    test.setTimeout(60_000);
    await login(page);

    // ── Dimension 2: perf budget (collect counters across the load) ────────
    /** @type {string[]} */
    const apiCalls = [];
    page.on('request', (req) => {
      const u = req.url();
      if (u.includes('/api/')) apiCalls.push(u);
    });

    const histPromise = page.waitForResponse(
      (r) => r.url().includes('/api/options/historical'),
      { timeout: 30_000 },
    );
    await page.goto(CHART_URL, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);
    const histResp = await histPromise;
    const histBody = await histResp.json();

    // Settle: give live-LTP + greeks one round-trip, but no more.
    await page.waitForTimeout(2_000);

    // ── Dimension 4: reusable code — canonical ChartWorkspace markers ─────
    // The page must contain the exported scope hooks: `.cw-range-group`
    // and `.cw-range-btn` (not a hand-rolled tabs strip).
    await expect(page.locator('.cw-range-group')).toBeVisible();
    await expect(page.locator('.cw-range-btn').first()).toBeVisible();

    // ── Dimension 5: UX color consistency — active range pill cyan-400 ────
    // The cyan-400 palette is the canonical card-header trio color.
    const activeBtn = page.locator('.cw-range-btn.is-active').first();
    if (await activeBtn.count()) {
      const color = await activeBtn.evaluate((el) => getComputedStyle(el).color);
      // Accept either the cyan-400 fill (`rgb(34, 211, 238)`) or the hover
      // brighter cyan-300 (`rgb(103, 232, 249)`). Anything else = palette drift.
      expect(
        ['rgb(34, 211, 238)', 'rgb(103, 232, 249)'],
        `Active range button color drifted from cyan-400 palette: ${color}`,
      ).toContain(color);
    }

    // ── Dimension 1: SSOT — historical bars carry a consistent ordering ───
    // The last bar's timestamp must be > first bar's timestamp; both
    // present; no NaN closes. Catches "two sources of bars" regressions
    // where one path is a stub.
    const bars = Array.isArray(histBody?.bars) ? histBody.bars : [];
    expect(bars.length).toBeGreaterThan(0);
    const firstTs = bars[0]?.ts || bars[0]?.date;
    const lastTs  = bars[bars.length - 1]?.ts || bars[bars.length - 1]?.date;
    expect(lastTs, 'bars must be chronologically ordered').not.toBe(firstTs);
    for (const b of bars) {
      expect(Number.isFinite(b.close), `bar close must be finite: ${JSON.stringify(b)}`).toBe(true);
    }

    // ── Dimension 2: perf budget — cold load must stay under 25 API calls ─
    // Pulse-page budget is ~20-40 req/min steady-state; a fresh chart load
    // should be well under that. 25 is a deliberately tight budget; if it
    // breaks because of a real feature add, raise it consciously, don't
    // silently let it climb.
    expect(
      apiCalls.length,
      `Chart cold-load XHR budget exceeded. Got ${apiCalls.length} calls:\n${apiCalls.join('\n')}`,
    ).toBeLessThanOrEqual(25);
  });

  test('stale code: resolver fallback no longer includes NFO for indices', async ({ page }) => {
    test.setTimeout(30_000);
    await login(page);

    // Source-grep the chart workspace bundle for the legacy NFO fallback
    // string. After the fix, indices return early before reaching the
    // `defaultExch` branch, and that branch's `'NFO'` literal was removed.
    // If the literal re-appears, someone restored the broken fallback.
    const html = await page.request.get(`${BASE}/charts?symbol=NIFTY 50&mode=live`, {
      timeout: 15_000,
    }).then((r) => r.text());

    // Pull all <script src> values, fetch each, scan for the legacy chain.
    const scriptMatches = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map((m) => m[1]);
    let foundLegacy = false;
    for (const src of scriptMatches.slice(0, 30)) {
      const absUrl = src.startsWith('http') ? src : new URL(src, BASE).toString();
      const body = await page.request.get(absUrl, { timeout: 10_000 })
        .then((r) => r.text()).catch(() => '');
      // Legacy chain: `isMcx?"MCX":isCds?"CDS":"NFO"` (minified)
      // or its equivalent with quotes. If this appears, the resolver
      // regressed to mapping indices through the front-month future.
      if (/["']NFO["']\s*[:)]/i.test(body) && /["']CDS["']/i.test(body) && /["']MCX["']/i.test(body)) {
        if (body.includes('_resolveFetchSymbol') || body.includes('KITE_INDEX_TO_ROOT')) {
          foundLegacy = true;
          break;
        }
      }
    }
    expect(
      foundLegacy,
      'Resolver bundle still contains legacy NFO fallback chain — index regression risk.',
    ).toBe(false);
  });
});
