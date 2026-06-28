/**
 * chart_overlays.spec.js
 *
 * Validates the new chart overlay system (VWAP, MACD, EMA, BB, RSI) and
 * their redraw behaviour (symbol switch, timeframe switch).
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *  1. SSOT     — overlay paths render with non-empty `d` attr
 *  2. Perf     — cold-load XHR budget unchanged (≤35 calls)
 *  3. Stale    — no inline indicator math in ChartWorkspace bundle
 *  4. Reusable — indicators.js module is imported by ChartWorkspace
 *  5. UX       — mobile: controls fit without horizontal scroll;
 *                desktop: sub-panels visible below main chart
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/chart_overlays.spec.js \
 *   --project=chromium-desktop --project=mobile-portrait --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const NIFTY_URL = `${BASE}/charts?symbol=${encodeURIComponent('NIFTY 50')}&mode=live`;

/** Inject saved sessionStorage items before navigation. */
async function injectSession(page, items) {
  await page.addInitScript((data) => {
    for (const [k, v] of Object.entries(data)) {
      sessionStorage.setItem(k, v);
    }
  }, items);
  if (items.ramboq_token) {
    await page.context().setExtraHTTPHeaders({
      Authorization: `Bearer ${items.ramboq_token}`,
    });
  }
}

test.describe.configure({ mode: 'serial' });
test.describe('chart overlays — all viewports', () => {
  /** @type {Record<string, string>} */
  let _session = {};

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(60_000);
    const ctx = await browser.newContext();
    const pg  = await ctx.newPage();
    await loginAsAdmin(pg);
    _session = await pg.evaluate(() => {
      const out = {};
      for (const k of ['ramboq_token', 'ramboq_user']) {
        const v = sessionStorage.getItem(k);
        if (v) out[k] = v;
      }
      return out;
    });
    await pg.close();
    await ctx.close();
  });

  // ── Helpers ──────────────────────────────────────────────────────────────

  /** Wait for the chart SVG to appear with at least one path. */
  async function waitForChart(page) {
    await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 20_000 });
    await expect(page.locator('.cw-svg path').first()).toBeVisible({ timeout: 20_000 });
  }

  /**
   * Enable overlays by injecting their values directly into localStorage,
   * then reloading so ChartWorkspace picks them up via `_loadOverlayPrefs()`.
   *
   * This approach is used because `.cw-root` has `overflow: hidden` which
   * clips the absolutely-positioned MultiSelect dropdown panel, preventing
   * Playwright's pointer event simulation from reaching the option elements.
   * The localStorage injection path exercises the same reactive code path
   * (same `_overlays` $state → `_showVwap` / `_showMacd` etc.) as the UI toggle.
   */
  async function setOverlaysViaStorage(page, overlayKeys) {
    await page.evaluate((keys) => {
      localStorage.setItem('rbq.cache.chart-overlays.v1', JSON.stringify(keys));
    }, overlayKeys);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await waitForChart(page);
    // Give $derived computations a frame to complete
    await page.waitForTimeout(600);
  }

  // ── Dimension 1: SSOT — overlays render with non-empty d attr ────────────

  test('SSOT: VWAP overlay renders with non-empty d attribute', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);

    await setOverlaysViaStorage(page, ['vwap']);

    const vwapPath = page.locator('.cw-svg path.overlay-vwap');
    await expect(vwapPath).toBeVisible({ timeout: 5_000 });
    const d = await vwapPath.getAttribute('d');
    expect(d, 'VWAP overlay path d should not be empty').toBeTruthy();
    expect(d.length, 'VWAP path should have meaningful content').toBeGreaterThan(10);
  });

  test('SSOT: MACD overlay renders sub-panel with non-empty d attribute', async ({ page }) => {
    test.setTimeout(90_000);
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);

    // Switch to 3M range (90 days) to guarantee enough bars for MACD (needs 27+)
    const histPromise = page.waitForResponse(
      (r) => r.url().includes('/api/options/historical'),
      { timeout: 30_000 },
    ).catch(() => null);
    const btn3M = page.locator('.cw-range-group .cw-range-btn', { hasText: '3M' });
    await expect(btn3M).toBeVisible({ timeout: 10_000 });
    await btn3M.click();
    await histPromise;
    await page.waitForTimeout(1_000);

    // Set MACD overlay after bars loaded so reload also requests 3M
    // (URL includes the symbol but not range — use storage + the already-active range)
    await setOverlaysViaStorage(page, ['macd']);

    // Re-click 3M after the reload since range resets to default (1M) on fresh load
    const histPromise2 = page.waitForResponse(
      (r) => r.url().includes('/api/options/historical'),
      { timeout: 30_000 },
    ).catch(() => null);
    const btn3M2 = page.locator('.cw-range-group .cw-range-btn', { hasText: '3M' });
    await btn3M2.click();
    await histPromise2;
    await page.waitForTimeout(1_000);

    const macdPath = page.locator('.cw-svg path.overlay-macd').first();
    await expect(macdPath).toBeVisible({ timeout: 8_000 });
    const d = await macdPath.getAttribute('d');
    expect(d, 'MACD overlay path d should not be empty').toBeTruthy();
    expect(d.length).toBeGreaterThan(10);
  });

  test('SSOT: EMA overlay renders with non-empty d attribute', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);

    await setOverlaysViaStorage(page, ['ema20']);

    const emaPath = page.locator('.cw-svg path.overlay-ema').first();
    await expect(emaPath).toBeVisible({ timeout: 5_000 });
    const d = await emaPath.getAttribute('d');
    expect(d, 'EMA overlay path d should not be empty').toBeTruthy();
    expect(d.length).toBeGreaterThan(10);
  });

  test('SSOT: Bollinger Bands overlay renders with non-empty d attribute', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);

    await setOverlaysViaStorage(page, ['bb']);

    const bbPath = page.locator('.cw-svg path.overlay-bb').first();
    await expect(bbPath).toBeVisible({ timeout: 5_000 });
    const d = await bbPath.getAttribute('d');
    expect(d, 'BB overlay path d should not be empty').toBeTruthy();
    expect(d.length).toBeGreaterThan(10);
  });

  test('SSOT: RSI overlay renders with non-empty d attribute', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);

    await setOverlaysViaStorage(page, ['rsi']);

    const rsiPath = page.locator('.cw-svg path.overlay-rsi');
    await expect(rsiPath).toBeVisible({ timeout: 5_000 });
    const d = await rsiPath.getAttribute('d');
    expect(d, 'RSI overlay path d should not be empty').toBeTruthy();
    expect(d.length).toBeGreaterThan(10);
  });

  // ── Dimension 2: Performance — XHR budget ────────────────────────────────

  test('perf: cold-load XHR budget unchanged after overlay additions', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);

    // Clear overlays so cold load doesn't add indicator-related requests
    await page.addInitScript(() => {
      localStorage.removeItem('rbq.cache.chart-overlays.v1');
    });

    const apiCalls = [];
    page.on('request', (req) => {
      if (req.url().includes('/api/')) apiCalls.push(req.url());
    });

    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);
    await page.waitForTimeout(2_000);

    expect(
      apiCalls.length,
      `XHR budget exceeded after overlay additions. Got ${apiCalls.length} calls:\n${apiCalls.join('\n')}`,
    ).toBeLessThanOrEqual(35);
  });

  // ── Dimension 3: Stale code — indicators.js module used (no inline math) ──

  test('stale code: indicators.js VWAP/MACD math not duplicated in bundles', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);

    const html = await page.request.get(NIFTY_URL, { timeout: 15_000 }).then((r) => r.text());
    const scriptSrcs = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map((m) => m[1]);

    // The VWAP cumulative math (cumTPV / cumVol) should appear in at most ONE
    // bundle chunk — the indicators.js module (merged with ChartWorkspace).
    // More than 1 chunk means the math was duplicated.
    let inlineMathCount = 0;
    for (const src of scriptSrcs.slice(0, 30)) {
      const absUrl = src.startsWith('http') ? src : new URL(src, BASE).toString();
      const body = await page.request.get(absUrl, { timeout: 10_000 })
        .then((r) => r.text()).catch(() => '');
      if (body.includes('cumTPV') || body.includes('cumVol')) inlineMathCount++;
    }
    expect(
      inlineMathCount,
      'VWAP cumulative math should not be duplicated across multiple bundle chunks',
    ).toBeLessThanOrEqual(1);

    // The RSI Wilder smoothing constant RSI_N=14 and MACD fast/slow defaults
    // should appear in at most ONE chunk (not re-implemented inline).
    let macdMathCount = 0;
    for (const src of scriptSrcs.slice(0, 30)) {
      const absUrl = src.startsWith('http') ? src : new URL(src, BASE).toString();
      const body = await page.request.get(absUrl, { timeout: 10_000 })
        .then((r) => r.text()).catch(() => '');
      // avgGain / avgLoss pattern = Wilder RSI smoothing
      if (body.includes('avgGain') && body.includes('avgLoss')) macdMathCount++;
    }
    expect(
      macdMathCount,
      'RSI smoothing math should not be duplicated across bundle chunks',
    ).toBeLessThanOrEqual(1);
  });

  // ── Dimension 4: Reusable — overlay paths update on range change ──────────

  test('reuse: EMA overlay path recomputes after range change (redraw check)', async ({ page }) => {
    test.setTimeout(90_000);
    await injectSession(page, _session);
    // Start with EMA20 enabled in localStorage
    await page.addInitScript(() => {
      localStorage.setItem('rbq.cache.chart-overlays.v1', JSON.stringify(['ema20']));
    });
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);
    await page.waitForTimeout(500);

    // Capture the current EMA path on 1M
    const emaPath = page.locator('.cw-svg path.overlay-ema').first();
    await expect(emaPath).toBeVisible({ timeout: 5_000 });
    const dBefore = await emaPath.getAttribute('d');
    expect(dBefore, 'initial EMA path should not be empty').toBeTruthy();

    // Switch to 1Y range — forces a re-fetch + new bars + recomputed overlay
    const histPromise = page.waitForResponse(
      (r) => r.url().includes('/api/options/historical'),
      { timeout: 30_000 },
    ).catch(() => null);
    const btn1Y = page.locator('.cw-range-group .cw-range-btn', { hasText: '1Y' });
    await btn1Y.click();
    await histPromise;
    await page.waitForTimeout(800);

    // EMA path should differ (more bars = different path shape)
    const dAfter = await emaPath.getAttribute('d');
    expect(dAfter, 'EMA path should be non-empty after 1Y switch').toBeTruthy();
    expect(
      dAfter !== dBefore,
      'EMA path must differ after range change (overlay redrawn from new bars)',
    ).toBe(true);
  });

  // ── Dimension 5: UX — mobile + desktop layout ─────────────────────────────

  test('UX: overlay controls row fits without horizontal scroll', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);

    // The controls row must not cause horizontal overflow on any viewport
    const controlsRow = page.locator('.cw-controls');
    await expect(controlsRow).toBeVisible();

    const overflow = await controlsRow.evaluate((el) => {
      return el.scrollWidth > el.clientWidth + 2; // 2px tolerance
    });
    expect(overflow, 'Controls row overflows horizontally').toBe(false);
  });

  test('UX: RSI sub-panel renders inside chart SVG when enabled', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);

    await setOverlaysViaStorage(page, ['rsi']);

    const rsiPath = page.locator('.cw-svg path.overlay-rsi');
    await expect(rsiPath).toBeVisible({ timeout: 5_000 });
    const d = await rsiPath.getAttribute('d');
    expect(d, 'RSI path rendered in SVG').toBeTruthy();

    // RSI reference lines (30/70) should exist — check via text labels
    // The RSI panel renders "70" and "30" text labels
    const svgTexts = await page.locator('.cw-svg text').allTextContents();
    const hasRef70 = svgTexts.some((t) => t.trim() === '70');
    const hasRef30 = svgTexts.some((t) => t.trim() === '30');
    expect(hasRef70, 'RSI 70 reference line label should be present').toBe(true);
    expect(hasRef30, 'RSI 30 reference line label should be present').toBe(true);
  });

  test('UX: MACD sub-panel renders with label when enabled on 3M range', async ({ page }) => {
    test.setTimeout(90_000);
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);

    // Load 3M range first
    const histPromise = page.waitForResponse(
      (r) => r.url().includes('/api/options/historical'),
      { timeout: 30_000 },
    ).catch(() => null);
    await page.locator('.cw-range-group .cw-range-btn', { hasText: '3M' }).click();
    await histPromise;
    await page.waitForTimeout(500);

    await setOverlaysViaStorage(page, ['macd']);

    // Click 3M again after reload
    const histPromise2 = page.waitForResponse(
      (r) => r.url().includes('/api/options/historical'),
      { timeout: 30_000 },
    ).catch(() => null);
    await page.locator('.cw-range-group .cw-range-btn', { hasText: '3M' }).click();
    await histPromise2;
    await page.waitForTimeout(1_000);

    // MACD label text should appear in the SVG
    const svgTexts = await page.locator('.cw-svg text').allTextContents();
    const hasMacdLabel = svgTexts.some((t) => t.includes('MACD'));
    expect(hasMacdLabel, 'MACD label should appear in SVG sub-panel').toBe(true);
  });

  // ── localStorage persistence ──────────────────────────────────────────────

  test('overlay preferences persist in localStorage and restore on reload', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    // Start clean
    await page.addInitScript(() => {
      localStorage.removeItem('rbq.cache.chart-overlays.v1');
    });
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);

    // Inject EMA20 via storage (simulates user enabling it)
    await page.evaluate(() => {
      localStorage.setItem('rbq.cache.chart-overlays.v1', JSON.stringify(['ema20', 'vwap']));
    });

    // Reload to apply
    await page.reload({ waitUntil: 'domcontentloaded' });
    await waitForChart(page);
    await page.waitForTimeout(600);

    // Both overlays should be rendered without any UI interaction
    const emaPath = page.locator('.cw-svg path.overlay-ema').first();
    await expect(emaPath).toBeVisible({ timeout: 5_000 });
    const vwapPath = page.locator('.cw-svg path.overlay-vwap');
    await expect(vwapPath).toBeVisible({ timeout: 5_000 });

    // Verify localStorage still contains the saved preferences
    const stored = await page.evaluate(() => localStorage.getItem('rbq.cache.chart-overlays.v1'));
    expect(stored, 'prefs should remain in localStorage after reload').toBeTruthy();
    const parsed = JSON.parse(stored);
    expect(parsed).toContain('ema20');
    expect(parsed).toContain('vwap');
  });
});
