/**
 * charts_equity_default.spec.js
 *
 * Regression spec for: /charts shows "No data available" when the default
 * symbol is RELIANCE (or any NSE equity not classified as a Kite index,
 * MCX commodity, or CDS currency).
 *
 * Root cause: _resolveFetchSymbol returned { sym, exch: '' } for plain
 * equities — empty exchange means the backend historical endpoint cannot
 * look up the instrument_token and returns no bars.
 *
 * Fix: added a plain-equity branch that routes alphabetic symbols with no
 * FUT/CE/PE suffix to exchange=NSE before the empty-exchange fallback.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *   1. SSOT   — /api/options/historical request carries exchange=NSE + symbol=RELIANCE
 *   2. Perf   — cold chart load fires ≤25 API requests
 *   3. Stale  — bundled JS no longer has the old `{ sym, exch: '' }` as the only
 *               fallthrough for non-special-case symbols (empty-exchange path is
 *               only reached when the symbol itself is non-alphabetic)
 *   4. Reuse  — chart uses canonical .cw-range-group
 *   5. UX     — active range button is cyan-400 (rgb(34, 211, 238)) or lighter
 *
 * Target: dev.ramboq.com
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/charts_equity_default.spec.js \
 *   --project=chromium-desktop --workers=1
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const login = loginAsAdmin;

// Helper: wait for the range-group strip to be visible (chart rendered).
async function waitForRangeGroup(page) {
  await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 25_000 });
}

// Parametrize over canonical NSE equities including a symbol with & so the
// regex `/^[A-Z][A-Z0-9&-]*$/` is explicitly exercised.
const EQUITIES = ['RELIANCE', 'TCS', 'HDFCBANK', 'M&M'];

test.describe('/charts — NSE equity default symbol fix', () => {

  // ── Dimension 1: SSOT — request URL carries exchange=NSE ────────────────
  for (const sym of EQUITIES) {
    test(`SSOT: ${sym} request carries exchange=NSE and returns bars`, async ({ page }) => {
      test.setTimeout(90_000);
      await login(page);

      const chartUrl = `${BASE}/charts?symbol=${encodeURIComponent(sym)}&mode=live`;

      // Register the waitForResponse BEFORE navigation.
      const histPromise = page.waitForResponse(
        (r) => r.url().includes('/api/options/historical'),
        { timeout: 40_000 },
      );

      await page.goto(chartUrl, { waitUntil: 'domcontentloaded' });
      await waitForRangeGroup(page);

      const histResp = await histPromise;

      // 1a. Request URL must carry the resolved exchange=NSE.
      const reqUrl = histResp.url();
      expect(
        reqUrl,
        `${sym}: URL must contain exchange=NSE, got: ${reqUrl}`,
      ).toMatch(/exchange=NSE/i);

      // 1b. Request URL must carry the correct symbol.
      const encodedSym = encodeURIComponent(sym);
      expect(
        reqUrl,
        `${sym}: URL must contain symbol=${sym}, got: ${reqUrl}`,
      ).toMatch(new RegExp(`symbol=${encodedSym}`, 'i'));

      // 1c. Response status 200.
      expect(histResp.status(), `${sym}: historical endpoint returned non-200`).toBe(200);

      // 1d. Response body carries >30 bars (enough trading days in a month).
      const body = await histResp.json();
      const bars = Array.isArray(body?.bars) ? body.bars : [];
      expect(
        bars.length,
        `${sym}: expected >30 bars, got ${bars.length}. ` +
        `Likely backend didn't find instrument or exchange was still empty.`,
      ).toBeGreaterThan(30);

      // 1e. No "No data available." error text visible on the page.
      const noDataText = await page.locator('text=No data available').count();
      expect(
        noDataText,
        `${sym}: page shows "No data available" — resolver returned empty exchange`,
      ).toBe(0);
    });
  }

  // ── Dimension 2: Perf — cold load ≤25 API requests ─────────────────────
  test('perf: RELIANCE cold chart load stays under 25 API calls', async ({ page }) => {
    test.setTimeout(60_000);
    await login(page);

    /** @type {string[]} */
    const apiCalls = [];
    page.on('request', (req) => {
      if (req.url().includes('/api/')) apiCalls.push(req.url());
    });

    const histPromise = page.waitForResponse(
      (r) => r.url().includes('/api/options/historical'),
      { timeout: 40_000 },
    );
    await page.goto(`${BASE}/charts?symbol=RELIANCE&mode=live`, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);
    await histPromise;

    // Allow one round-trip for live-LTP / greeks to settle.
    await page.waitForTimeout(2_000);

    expect(
      apiCalls.length,
      `Chart cold-load XHR budget exceeded. Got ${apiCalls.length} calls:\n${apiCalls.join('\n')}`,
    ).toBeLessThanOrEqual(25);
  });

  // ── Dimension 3: Stale code — empty-exchange path is no longer the only
  //    fallthrough for plain equities. We grep the bundled JS to confirm
  //    the fix landed: the string "exch:''" (or "exch: ''") must NOT appear
  //    as the sole resolution path for an alphabetic symbol without
  //    the FUT/CE/PE guard that now precedes it.
  //    Specifically: the old one-liner `if (!root) return { sym, exch: '' }`
  //    is gone; the new branch has `_isPlainEquity` before the empty-exch
  //    return. We assert the bundle contains "isPlainEquity" (or minified
  //    variant "PlainEquity" or the regex literal from the fix).
  test('stale code: bundle contains plain-equity NSE routing branch', async ({ page }) => {
    test.setTimeout(30_000);
    await login(page);

    const html = await page.request.get(
      `${BASE}/charts?symbol=RELIANCE&mode=live`,
      { timeout: 15_000 },
    ).then((r) => r.text());

    // Pull <script src> values and scan each JS bundle.
    const scriptSrcs = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map((m) => m[1]);
    let foundEquityBranch = false;
    for (const src of scriptSrcs.slice(0, 40)) {
      const absUrl = src.startsWith('http') ? src : new URL(src, BASE).toString();
      const body = await page.request.get(absUrl, { timeout: 10_000 })
        .then((r) => r.text()).catch(() => '');
      // The fix introduces either the variable name "_isPlainEquity" or
      // the regex literal /FUT|CE|PE/ adjacent to a return with 'NSE'.
      // Any of these tokens confirm the equity-routing branch is present.
      if (
        body.includes('isPlainEquity') ||
        body.includes('PlainEquity') ||
        (body.includes('FUT|CE|PE') && body.includes("exch:'NSE'")) ||
        (body.includes('FUT|CE|PE') && body.includes('exch:"NSE"'))
      ) {
        foundEquityBranch = true;
        break;
      }
    }
    expect(
      foundEquityBranch,
      'Bundle does not contain the plain-equity NSE routing branch — fix may not have been compiled in.',
    ).toBe(true);
  });

  // ── Dimensions 4 + 5: reusable component + UX palette ───────────────────
  test('reuse + palette: .cw-range-group present, active btn is cyan-400', async ({ page }) => {
    test.setTimeout(60_000);
    await login(page);
    await page.goto(`${BASE}/charts?symbol=RELIANCE&mode=live`, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);

    // Dimension 4: canonical range-group is used (not a hand-rolled strip).
    await expect(page.locator('.cw-range-group')).toBeVisible();
    await expect(page.locator('.cw-range-btn').first()).toBeVisible();

    // Dimension 5: active range button carries cyan-400 colour.
    const activeBtn = page.locator('.cw-range-btn.is-active').first();
    if (await activeBtn.count()) {
      const color = await activeBtn.evaluate((el) => getComputedStyle(el).color);
      expect(
        ['rgb(34, 211, 238)', 'rgb(103, 232, 249)'],
        `Active range button color drifted from cyan-400 palette: ${color}`,
      ).toContain(color);
    }
  });
});
