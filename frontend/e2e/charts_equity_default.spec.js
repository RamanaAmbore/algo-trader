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
 *   1. SSOT   — chart renders bars (no "No data available") for NSE equities
 *   2. Perf   — cold chart load fires <= 40 API requests
 *   3. Stale  — source file contains the _isPlainEquity branch + exch:'NSE' fix
 *   4. Reuse  — chart uses canonical .cw-range-group component
 *   5. UX     — active range button is cyan-400 (rgb(34, 211, 238)) or lighter
 *
 * Target: dev.ramboq.com
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/charts_equity_default.spec.js \
 *   --project=chromium-desktop --workers=1
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

// Path to the ChartWorkspace source for stale-code dimension.
const __dir = dirname(fileURLToPath(import.meta.url));
const _CW_SRC = join(__dir, '../src/lib/ChartWorkspace.svelte');

// Helper: wait for the range-group strip to be visible (chart rendered).
async function waitForRangeGroup(page) {
  await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 25_000 });
}

/**
 * Wait until either the chart renders visible SVG paths or "No data available"
 * appears on screen.  Returns 'data' | 'error' | 'timeout'.
 */
async function waitForChartResult(page) {
  try {
    await page.waitForFunction(
      () => {
        // Rendered chart: at least one SVG path with a real d= attribute.
        const paths = document.querySelectorAll('svg path[d]');
        for (const p of paths) {
          if ((p.getAttribute('d') || '').length > 20) return true;
        }
        // Error state.
        if (document.body.innerText.includes('No data available')) return true;
        return false;
      },
      { timeout: 35_000 },
    );
    const noData = await page.locator('text=No data available').count();
    return noData > 0 ? 'error' : 'data';
  } catch (_) {
    return 'timeout';
  }
}

// Parametrize over canonical NSE equities including a symbol with & so the
// regex /^[A-Z][A-Z0-9&-]*$/ is explicitly exercised.
const EQUITIES = ['RELIANCE', 'TCS', 'HDFCBANK', 'M&M'];

// Run serially to avoid parallel login rate-limit pressure.
test.describe.configure({ mode: 'serial' });

test.describe('/charts — NSE equity default symbol fix', () => {

  // ── Dimension 1: SSOT — chart renders bars for each NSE equity ──────────
  for (const sym of EQUITIES) {
    test(`SSOT: ${sym} — chart renders data, no "No data available"`, async ({ page }) => {
      test.setTimeout(120_000);
      await loginAsAdmin(page);

      const chartUrl = `${BASE}/charts?symbol=${encodeURIComponent(sym)}&mode=live`;

      // Collect ALL historical responses so we can inspect whichever one
      // carries the resolved exchange.  (The first response may come from
      // prefetchChartBars which fires without an exchange param; the second
      // comes from _loadHistorical which runs _resolveFetchSymbol first.)
      /** @type {string[]} */
      const histUrls = [];
      page.on('response', (r) => {
        if (r.url().includes('/api/options/historical')) histUrls.push(r.url());
      });

      await page.goto(chartUrl, { waitUntil: 'domcontentloaded' });
      await waitForRangeGroup(page);

      // Primary assertion: chart renders data, not an error.
      const result = await waitForChartResult(page);
      expect(
        result,
        `${sym}: chart result was "${result}" — expected "data". ` +
        `If "error": _resolveFetchSymbol returned empty exchange. ` +
        `If "timeout": chart never rendered within 35s.`,
      ).toBe('data');

      // Secondary: no error text visible.
      await expect(
        page.locator('text=No data available'),
        `${sym}: "No data available" text visible — resolver returned empty exchange`,
      ).not.toBeVisible();

      // Informational: if any network call was made for this symbol,
      // confirm at least one carried exchange=NSE (the resolved exchange).
      // Background prefetch calls for other symbols (watchlist, chain) are
      // filtered out by only considering URLs that also contain this symbol.
      const symUrls = histUrls.filter((u) =>
        new RegExp(`symbol=${encodeURIComponent(sym)}`, 'i').test(u),
      );
      if (symUrls.length > 0) {
        const hasNseExch = symUrls.some((u) => /exchange=NSE/i.test(u));
        expect(
          hasNseExch,
          `${sym}: historical call for this symbol was made but no URL carried exchange=NSE.\n` +
          `Calls seen:\n${symUrls.join('\n')}`,
        ).toBe(true);
      }
    });
  }

  // ── Dimension 2: Perf — cold load <=40 API requests ─────────────────────
  // The dev charts page cold-loads watchlist, pinned symbols, auth status,
  // and chart data in parallel — a fresh session with no cache fires ~30-36
  // requests. 40 is a tight budget that catches regressions without being
  // fragile to adding one new ping.
  test('perf: RELIANCE cold chart load stays under 40 API calls', async ({ page }) => {
    test.setTimeout(90_000);
    await loginAsAdmin(page);

    /** @type {string[]} */
    const apiCalls = [];
    page.on('request', (req) => {
      if (req.url().includes('/api/')) apiCalls.push(req.url());
    });

    // Wait for the historical response before measuring (ensure cold load
    // has fully settled, not mid-flight).
    const histPromise = page.waitForResponse(
      (r) => r.url().includes('/api/options/historical'),
      { timeout: 45_000 },
    );
    await page.goto(`${BASE}/charts?symbol=RELIANCE&mode=live`, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);
    await histPromise;

    // Allow one round-trip for live-LTP / greeks to settle.
    await page.waitForTimeout(2_000);

    expect(
      apiCalls.length,
      `Chart cold-load XHR budget exceeded. Got ${apiCalls.length} calls:\n${apiCalls.join('\n')}`,
    ).toBeLessThanOrEqual(40);
  });

  // ── Dimension 3: Stale code — source contains the equity routing branch ──
  // Read the local ChartWorkspace.svelte source directly.  The minifier
  // inlines the single-use _isPlainEquity variable so it is invisible in
  // compiled bundles; source-level check is authoritative.
  test('stale code: ChartWorkspace source contains plain-equity NSE branch', () => {
    let src;
    try {
      src = readFileSync(_CW_SRC, 'utf-8');
    } catch (e) {
      throw new Error(`Could not read ChartWorkspace.svelte at ${_CW_SRC}: ${e.message}`);
    }

    // All three tokens must be present together to confirm the fix.
    expect(
      src.includes('_isPlainEquity'),
      'Source is missing the _isPlainEquity variable — equity routing fix not present',
    ).toBe(true);

    expect(
      src.includes("exch: 'NSE'"),
      "Source is missing the `exch: 'NSE'` return — equity routing fix incomplete",
    ).toBe(true);

    expect(
      src.includes('/^[A-Z][A-Z0-9&-]*$/'),
      'Source is missing the plain-equity regex — equity routing fix incomplete',
    ).toBe(true);
  });

  // ── Dimensions 4 + 5: reusable component + UX palette ───────────────────
  test('reuse + palette: .cw-range-group present, active btn is cyan-400', async ({ page }) => {
    test.setTimeout(90_000);
    await loginAsAdmin(page);
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
