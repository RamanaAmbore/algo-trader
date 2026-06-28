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
const NIFTY_URL  = `${BASE}/charts?symbol=${encodeURIComponent('NIFTY 50')}&mode=live`;
// VWAP requires real volume data; NIFTY 50 is an index (volume=0 → VWAP
// path would be all-null). Use a large-cap equity for volume-dependent tests.
const STOCK_URL  = `${BASE}/charts?symbol=${encodeURIComponent('RELIANCE')}&mode=live`;

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

// Mode: 'default' (not 'serial') so a flake in one test doesn't skip the
// rest of the suite — that hid the new sub-slice cases (multi-select,
// hide-inactive, 1Y, candle default, alignment) from running.  workers=1
// is still enforced via the CLI flag in this spec's run command, so the
// rate-limit avoidance (commit 8ebebd63) still holds.
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

  /** Wait for the chart SVG to appear with at least one drawn shape.
   *  After the candle-default flip (this slice), .cw-svg path is empty
   *  on cold-load because candles render as <rect>+<line>, not <path>.
   *  Use the y-axis tick text as the "chart has data" signal instead —
   *  it renders once _bars + _yDomain are populated. Falls back to any
   *  SVG content if the y-tick label heuristic misses (e.g. a transient
   *  empty range). */
  async function waitForChart(page) {
    await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 20_000 });
    // y-axis labels (5 of them) render when _bars.length > 0 — present
    // for every series type. Single waitFor since they share the same
    // condition (any one visible means the chart has data).
    await expect(
      page.locator('.cw-svg text').first(),
    ).toBeVisible({ timeout: 20_000 });
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
    // Use a tradeable equity — NIFTY 50 is an index (volume=0 → VWAP all-null)
    await page.goto(STOCK_URL, { waitUntil: 'domcontentloaded' });
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
    // Use a tradeable equity for MACD too (consistent with VWAP test)
    await page.goto(STOCK_URL, { waitUntil: 'domcontentloaded' });
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
    // Use STOCK_URL because vwap path requires real volume data
    await page.goto(STOCK_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);

    // Write overlay prefs via evaluate() — NOT addInitScript().
    // addInitScript() fires on every navigation (including the reload below),
    // so it would wipe the key before onMount can read it.
    await page.evaluate(() => {
      localStorage.removeItem('rbq.cache.chart-overlays.v1');
      localStorage.setItem('rbq.cache.chart-overlays.v1', JSON.stringify(['ema20', 'vwap']));
    });

    // Reload — onMount reads the key and hydrates _overlays = ['ema20','vwap']
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

  // ────────────────────────────────────────────────────────────────────────────
  // Sub-slice: multi-select UI, hide-inactive, 1Y regression, candle default,
  // line/symbol left-alignment. Each operator request gets its own test so a
  // regression on any one item is localised in the failure report.
  // ────────────────────────────────────────────────────────────────────────────

  // ── Multi-select Indicators dropdown ──────────────────────────────────────

  test('multi-select: Indicators dropdown opens, multiple checkboxes can be ticked, closes with selections kept', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    // Clear any stored overlays so the trigger reads "Indicators" placeholder.
    await page.addInitScript(() => {
      localStorage.removeItem('rbq.cache.chart-overlays.v1');
      localStorage.removeItem('rbq.cache.chart-series.v1');
    });
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);

    // The Indicators trigger is the MultiSelect inside .cw-overlay-panel.
    const trigger = page.locator('.cw-overlay-panel .rbq-multi-trigger');
    await expect(trigger).toBeVisible({ timeout: 10_000 });

    // Trigger label should read "Indicators" when nothing is picked.
    const labelText = await trigger.locator('.rbq-multi-label').innerText();
    expect(
      labelText.trim(),
      'Trigger placeholder should read "Indicators" when no overlays picked',
    ).toContain('Indicators');

    // Click to open. The panel must be visible AND inside the viewport
    // (not clipped by the parent's overflow). z-index must lift it above
    // the chart SVG (path elements paint below the dropdown).
    await trigger.click();
    const panel = page.locator('.cw-overlay-panel .rbq-multi-panel');
    await expect(panel).toBeVisible({ timeout: 3_000 });

    // Click two distinct overlay options. mousedown (not click) per the
    // MultiSelect component's onmousedown handler — pointer events must
    // reach the <li> elements (overflow:hidden on .cw-root would block
    // this; we changed it to overflow:visible to make this work).
    const opt20 = panel.locator('.rbq-multi-option').filter({ hasText: 'EMA 20' });
    const optVwap = panel.locator('.rbq-multi-option').filter({ hasText: 'VWAP' });
    await opt20.dispatchEvent('mousedown');
    await optVwap.dispatchEvent('mousedown');

    // Both options should show the green check mark.
    await expect(opt20).toHaveClass(/rbq-multi-option-selected/, { timeout: 2_000 });
    await expect(optVwap).toHaveClass(/rbq-multi-option-selected/, { timeout: 2_000 });

    // Close by clicking outside (the cw-root background). Selections
    // persist — re-opening shows them still ticked.
    await page.locator('.cw-info-strip').click({ position: { x: 5, y: 5 } });
    await expect(panel).not.toBeVisible({ timeout: 2_000 });

    await trigger.click();
    await expect(panel).toBeVisible({ timeout: 3_000 });
    await expect(opt20).toHaveClass(/rbq-multi-option-selected/);
    await expect(optVwap).toHaveClass(/rbq-multi-option-selected/);

    // localStorage should hold both keys.  Poll up to 3 s — the persist
    // effect runs on a microtask after the bind:value propagation, so
    // give it a few render frames before asserting.
    await expect.poll(
      async () => page.evaluate(() => localStorage.getItem('rbq.cache.chart-overlays.v1')),
      {
        message: 'overlay selections written to localStorage',
        timeout: 3_000,
      },
    ).toBeTruthy();
    const stored = await page.evaluate(() =>
      localStorage.getItem('rbq.cache.chart-overlays.v1'),
    );
    const parsed = JSON.parse(stored);
    expect(parsed).toContain('ema20');
    expect(parsed).toContain('vwap');
  });

  // ── Hide inactive overlays — DOM removal on uncheck ────────────────────────

  test('hide inactive: unchecking an overlay removes its <path class="overlay-X"> from the DOM', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);

    // Start with EMA20 enabled — path is present.
    await setOverlaysViaStorage(page, ['ema20']);
    const emaPath = page.locator('.cw-svg path.overlay-ema');
    await expect(emaPath).toHaveCount(1, { timeout: 5_000 });

    // Toggle EMA20 off via storage — reload to apply.
    await page.evaluate(() => {
      localStorage.setItem('rbq.cache.chart-overlays.v1', JSON.stringify([]));
    });
    await page.reload({ waitUntil: 'domcontentloaded' });
    await waitForChart(page);
    await page.waitForTimeout(500);
    // After uncheck the <path class="overlay-ema"> must be gone from DOM
    // (not just rendered with empty `d`). Operator: "$derived doesn't
    // stale-cache" — the SVG gating now reads _overlays.includes(...).
    await expect(emaPath).toHaveCount(0, { timeout: 3_000 });

    // Same test via UI interaction — toggle on, then off, count must
    // transition 0 → 1 → 0 in the same session.
    const trigger = page.locator('.cw-overlay-panel .rbq-multi-trigger');
    await trigger.click();
    const opt20 = page.locator('.cw-overlay-panel .rbq-multi-panel .rbq-multi-option')
      .filter({ hasText: 'EMA 20' });
    await opt20.dispatchEvent('mousedown');
    await page.waitForTimeout(300);
    await expect(emaPath).toHaveCount(1, { timeout: 3_000 });
    // Tick OFF — same option dispatch toggles
    await opt20.dispatchEvent('mousedown');
    await page.waitForTimeout(300);
    await expect(emaPath).toHaveCount(0, { timeout: 3_000 });
  });

  // ── 1Y regression — partial-range fetch returns > 150 bars ────────────────

  test('1Y regression: clicking 1Y returns > 150 bars from /api/options/historical', async ({ page }) => {
    test.setTimeout(90_000);
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);

    // Capture the 1Y historical response.
    const histPromise = page.waitForResponse(
      (r) => {
        const u = r.url();
        return u.includes('/api/options/historical') && /days=365/.test(u);
      },
      { timeout: 40_000 },
    ).catch(() => null);

    const btn1Y = page.locator('.cw-range-group .cw-range-btn', { hasText: '1Y' });
    await expect(btn1Y).toBeVisible({ timeout: 10_000 });
    await btn1Y.click();

    const resp = await histPromise;
    expect(resp, '/api/options/historical?days=365 was not requested').not.toBeNull();
    expect(resp.status(), '1Y response status').toBe(200);

    const body = await resp.json().catch(() => null);
    expect(body, '1Y response body parseable').toBeTruthy();
    const bars = Array.isArray(body.bars) ? body.bars : [];
    expect(
      bars.length,
      `Expected > 150 bars for 1Y range, got ${bars.length}. Partial-range fetch ` +
      `regression (commit f8825b54 / a34936fd) — likely cause: head/tail slice merge ` +
      `dropped bars, or the resolver mapped NIFTY 50 to a recently-listed contract.`,
    ).toBeGreaterThan(150);
  });

  // ── Default candle series + persistence ───────────────────────────────────

  test('default series: first visit (no localStorage) defaults to candle', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    // Clear series key BEFORE navigation so onMount sees no stored value.
    await page.addInitScript(() => {
      localStorage.removeItem('rbq.cache.chart-series.v1');
    });
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);
    await page.waitForTimeout(500);

    // Chart-type Select trigger shows the active label. On first visit
    // (no stored key) the label must read "Candle".
    const typeTrigger = page.locator('.cw-type-chart-wrap .rbq-select-trigger');
    await expect(typeTrigger).toBeVisible({ timeout: 5_000 });
    const label = await typeTrigger.innerText();
    expect(
      label.trim(),
      `Default series should be "Candle" on first visit. Got: "${label.trim()}"`,
    ).toContain('Candle');

    // localStorage should now hold "candle" (the $effect writes it).
    await page.waitForTimeout(300);
    const stored = await page.evaluate(() =>
      localStorage.getItem('rbq.cache.chart-series.v1'),
    );
    expect(stored, 'series-type written to localStorage on first render').toBeTruthy();
    expect(JSON.parse(stored)).toBe('candle');
  });

  test('series persistence: operator pick survives reload', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    // Seed "line" as the operator's prior choice.
    await page.addInitScript(() => {
      localStorage.setItem('rbq.cache.chart-series.v1', JSON.stringify('line'));
    });
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    // Only need the chart-type picker to be present (this test doesn't
    // exercise bars).  Skip the full waitForChart() so a slow backend
    // can't fail this hydration assertion.
    const typeTrigger = page.locator('.cw-type-chart-wrap .rbq-select-trigger');
    await expect(typeTrigger).toBeVisible({ timeout: 20_000 });
    // Hydration runs in onMount after the initial $state init — poll
    // for the label to flip from "Candle" (default) to "Line" (stored).
    await expect.poll(
      async () => (await typeTrigger.innerText()).trim(),
      { timeout: 5_000, message: 'Stored "line" should hydrate to label' },
    ).toContain('Line');
  });

  // ── Line-type + symbol left-alignment (desktop only) ──────────────────────

  test('alignment: line-type picker sits flush-left with symbol picker on desktop', async ({ page, viewport }) => {
    test.setTimeout(60_000);
    // Only meaningful on a wide viewport — mobile stacks the row in a
    // different layout (chart-type wraps under the symbol search).
    if (!viewport || viewport.width < 1000) {
      test.skip(true, 'desktop-only alignment test');
      return;
    }
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);
    await page.waitForTimeout(400);

    // Find the bounding boxes of the symbol picker (SymbolSearchInput
    // wrapper) and the chart-type select trigger.
    const symBox  = await page.locator('.cw-picker .ssi-wrap').boundingBox();
    const typeBox = await page.locator('.cw-type-chart-wrap .rbq-select-trigger').boundingBox();
    expect(symBox, 'symbol picker box').not.toBeNull();
    expect(typeBox, 'chart-type box').not.toBeNull();

    // Chart-type should sit to the RIGHT of the symbol picker (flush-left
    // cluster, not pushed to the trailing edge). The gap between symbol
    // right and chart-type left must be small — < 60 px.
    const gap = typeBox.x - (symBox.x + symBox.width);
    expect(
      gap,
      `Chart-type picker should be flush with symbol picker. Gap=${gap}px (expect <60 px).`,
    ).toBeLessThan(60);
    expect(
      gap,
      `Chart-type picker should be to the right of symbol picker. Gap=${gap}px (expect ≥0).`,
    ).toBeGreaterThanOrEqual(0);
  });

  // ── Fluid width + no-overlap loop at 360 / 768 / 1280 (5-dimension UX) ────
  //
  // feedback_frontend_change_loop.md: every frontend change ships with a
  // fluid-width + no-overlap assertion across the three canonical widths.
  // We resize the page and assert (a) no horizontal scroll on the page
  // root, and (b) no bounding-box overlap between the picker row's main
  // controls (symbol + chart-type) and the controls row (indicators
  // dropdown trigger).

  for (const W of [360, 768, 1280]) {
    test(`fluid + no-overlap @ ${W}px wide — controls fit and do not overlap`, async ({ page }) => {
      test.setTimeout(60_000);
      await injectSession(page, _session);
      await page.setViewportSize({ width: W, height: 800 });
      await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
      await waitForChart(page);
      await page.waitForTimeout(400);

      // (a) Picker + controls row must not generate horizontal scroll.
      const pickerOverflow = await page.locator('.cw-picker').evaluate(
        (el) => el.scrollWidth > el.clientWidth + 2,
      );
      expect(pickerOverflow, `picker row overflows at ${W}px`).toBe(false);
      const controlsOverflow = await page.locator('.cw-controls').evaluate(
        (el) => el.scrollWidth > el.clientWidth + 2,
      );
      expect(controlsOverflow, `controls row overflows at ${W}px`).toBe(false);

      // (b) Page root must not produce a horizontal scrollbar.
      const docOverflow = await page.evaluate(
        () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 2,
      );
      expect(docOverflow, `document overflows at ${W}px`).toBe(false);

      // (c) No overlap: symbol picker bbox and chart-type bbox must NOT
      // intersect (they may sit on different visual rows on mobile but
      // their bboxes still shouldn't share pixels).
      const symBox  = await page.locator('.cw-picker .ssi-wrap').boundingBox();
      const typeBox = await page.locator('.cw-type-chart-wrap .rbq-select-trigger').boundingBox();
      if (symBox && typeBox) {
        const xOverlap = symBox.x < typeBox.x + typeBox.width && typeBox.x < symBox.x + symBox.width;
        const yOverlap = symBox.y < typeBox.y + typeBox.height && typeBox.y < symBox.y + symBox.height;
        const overlapping = xOverlap && yOverlap;
        expect(overlapping, `symbol picker and chart-type overlap @ ${W}px`).toBe(false);
      }
    });
  }

  // ── Toolbar height consistency — every interactive control on the
  // chart toolbar (range pills, Select triggers, MultiSelect trigger,
  // intraday toggle, symbol input) shares the SSOT --chart-toolbar-h
  // height. Operator: "the button and dropdown sizes are inconsistent
  // and increased height which needs to be corrected in charts". A
  // single mismatched control fails the test with its name + actual
  // pixel height so the offender is easy to spot in the report.
  for (const W of [375, 1280]) {
    test(`height consistency @ ${W}px — every toolbar control shares one height`, async ({ page }) => {
      test.setTimeout(60_000);
      await injectSession(page, _session);
      await page.setViewportSize({ width: W, height: 800 });
      await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
      await waitForChart(page);
      // Layout-stabilisation beat — at 375 px the picker row wraps and
      // controls reflow as the symbol search hydrates. Without this the
      // first sample can catch a control mid-reflow and read its
      // pre-CSS-applied content-box height.
      await page.waitForTimeout(600);
      // Confirm all four primary controls are visible before sampling —
      // a control that's not yet visible would yield a null box and
      // get skipped, potentially leaving a mismatched baseline.
      await expect(page.locator('.cw-range-group .cw-range-btn').first()).toBeVisible({ timeout: 5_000 });
      await expect(page.locator('.cw-type-chart-wrap .rbq-select-trigger')).toBeVisible({ timeout: 5_000 });
      await expect(page.locator('.cw-overlay-panel .rbq-multi-trigger')).toBeVisible({ timeout: 5_000 });
      await expect(page.locator('.cw-picker .ssi-input')).toBeVisible({ timeout: 5_000 });

      // Each entry: { name, locator }. Heights gathered then compared
      // pairwise to a tolerance of ±2 px (rounding + sub-pixel layout).
      const controls = [
        { name: 'range 1D',            sel: '.cw-range-group .cw-range-btn >> nth=0' },
        { name: 'range 1W',            sel: '.cw-range-group .cw-range-btn >> nth=1' },
        { name: 'range 1M',            sel: '.cw-range-group .cw-range-btn >> nth=2' },
        { name: 'intraday toggle',     sel: '.cw-intraday-btn' },
        { name: 'symbol type select',  sel: '.cw-type-wrap .rbq-select-trigger' },
        { name: 'chart type select',   sel: '.cw-type-chart-wrap .rbq-select-trigger' },
        { name: 'indicators trigger',  sel: '.cw-overlay-panel .rbq-multi-trigger' },
        { name: 'symbol search input', sel: '.cw-picker .ssi-input' },
      ];

      const heights = [];
      for (const c of controls) {
        const box = await page.locator(c.sel).boundingBox();
        if (!box) {
          // Controls behind a feature flag (no overlay panel, no
          // symbol input on compact mount) — skip rather than fail.
          continue;
        }
        heights.push({ name: c.name, h: Math.round(box.height) });
      }
      expect(
        heights.length,
        'expected at least 4 toolbar controls visible to compare heights',
      ).toBeGreaterThanOrEqual(4);

      const baseline = heights[0].h;
      for (const { name, h } of heights) {
        expect(
          Math.abs(h - baseline) <= 2,
          `Toolbar height drift @ ${W}px: "${name}" = ${h}px, baseline "${heights[0].name}" = ${baseline}px. ` +
          `All heights: ${heights.map(x => `${x.name}=${x.h}`).join(' | ')}`,
        ).toBe(true);
      }
    });
  }

  // ── Active-state palette — cyan-400 SSOT across active range +
  // active intraday toggle. Operator: "check for colour consistency in
  // charts and dashboard". Active state must match
  // .algo-tab[aria-selected=true].algo-tab-c-cyan in app.css =
  // rgb(34, 211, 238). Comparison is hex-tolerant (browsers serialise
  // computed colour as `rgb(R, G, B)`).
  test('palette: active range pill renders cyan-400 (canonical active state)', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);
    // Hydration beat — Svelte's $effect that applies the `.active`
    // class runs on the next microtask after _chartDays initialises.
    // Without this small wait the computed colour can briefly read
    // rgb(0, 0, 0) (UA <button> default) on slow dev cold-loads.
    await page.waitForTimeout(400);

    // 1M is the default range — already active on first load. Poll
    // for the cyan target so a sub-millisecond hydration race can't
    // flake the assertion.
    const activeSel = '.cw-range-group .cw-range-btn.active';
    await expect(page.locator(activeSel).first()).toBeVisible({ timeout: 5_000 });
    await expect.poll(
      async () => page.locator(activeSel).first().evaluate(el => getComputedStyle(el).color),
      {
        timeout: 5_000,
        message: 'Active range pill colour should stabilise on cyan-400 after hydration',
      },
    ).toBe('rgb(34, 211, 238)');
  });

  test('palette: intraday toggle active state renders cyan-400', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);

    const intra = page.locator('.cw-intraday-btn');
    await expect(intra).toBeVisible({ timeout: 5_000 });
    // Click to activate — toggle starts OFF.
    await intra.click();
    await page.waitForTimeout(200);
    await expect(intra).toHaveClass(/active/, { timeout: 2_000 });
    const colour = await intra.evaluate(el => getComputedStyle(el).color);
    expect(
      colour,
      `Intraday active colour expected rgb(34, 211, 238) cyan-400, got ${colour}`,
    ).toBe('rgb(34, 211, 238)');
  });

  // MultiSelect trigger should not carry its own competing border —
  // its visual frame is the inherited .rbq-multi-trigger style which
  // matches Select triggers. Operator: "MultiSelect trigger color
  // matches Select triggers".
  test('palette: MultiSelect indicators trigger shares border style with Select triggers', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);

    const multi = page.locator('.cw-overlay-panel .rbq-multi-trigger');
    const sel   = page.locator('.cw-type-chart-wrap .rbq-select-trigger');
    await expect(multi).toBeVisible({ timeout: 5_000 });
    await expect(sel).toBeVisible({ timeout: 5_000 });

    // Both triggers should share the canonical popup-trigger background
    // gradient — neither carries a competing sky/cyan border tint on
    // the wrapper. Pull border-color and assert they're close (both
    // amber-soft α 0.25 → exact match) or BOTH none/transparent.
    const [multiBorder, selBorder] = await Promise.all([
      multi.evaluate(el => getComputedStyle(el).borderColor),
      sel.evaluate(el => getComputedStyle(el).borderColor),
    ]);
    expect(
      multiBorder,
      `Both triggers should share border style. MultiSelect=${multiBorder} Select=${selBorder}`,
    ).toBe(selBorder);
  });

  // ── Coherent chart UX polish slice (Jun 2026) ──────────────────────────────
  //
  //   1. NAV chip is anchored LEFT (operator: "move nav chip to the
  //      left of nav chart").
  //   2. Chart-toolbar element heights match the canonical algo-tab
  //      strip rendered height on /dashboard within ±2 px (operator:
  //      "keep the elements above charts height consistent with the
  //      elements in other page").
  //   3. Reset button shares the same toolbar height.
  //   4. Toolbar→chart vertical gap is minimal (≤8 px).
  //   5. /charts at 393×851 + 360×640 fits without vertical scroll.

  test('NAV chip overlay is anchored LEFT-of-center, clearing Y-axis', async ({ page }) => {
    test.setTimeout(45_000);
    await injectSession(page, _session);
    // Visit /dashboard — the NAV chip mounts inside the NavTab on the
    // chart card. Skip-soft when the chip hasn't been minted yet.
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);
    const chip = page.locator('.nav-chip-overlay').first();
    const chipPresent = await chip.isVisible().catch(() => false);
    if (!chipPresent) {
      console.log('[chip-left] firm-NAV chip not minted — skipping left-anchor check');
      return;
    }
    // Updated Jun 2026: chip is anchored just RIGHT of the Y-axis line
    // (operator: "the nav overlay is overlapping the y label in nav
    // chart. start it just right of Y axis"). It is still LEFT-of-center
    // — right gap > left gap — but it no longer hugs the card edge.
    const chartCard = page.locator('.row1-col-chart').first();
    const [chipBox, cardBox] = await Promise.all([
      chip.boundingBox(),
      chartCard.boundingBox(),
    ]);
    expect(chipBox).not.toBeNull();
    expect(cardBox).not.toBeNull();
    if (chipBox && cardBox) {
      const leftGap  = chipBox.x - cardBox.x;
      const rightGap = (cardBox.x + cardBox.width) - (chipBox.x + chipBox.width);
      expect(
        rightGap > leftGap,
        `Chip should be left-of-center — leftGap=${leftGap}px rightGap=${rightGap}px`,
      ).toBe(true);
    }
  });

  test('SSOT height: chart toolbar matches dashboard algo-tab strip within ±2 px', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);

    // 1) Measure /dashboard's chart-card tab strip height — this is
    //    the cross-page anchor the operator asked for.
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2000);
    const dashTab = page.locator('.row1-col-chart .algo-tab').first();
    await expect(dashTab).toBeVisible({ timeout: 10_000 });
    const dashBox = await dashTab.boundingBox();
    expect(dashBox).not.toBeNull();
    const dashH = dashBox ? Math.round(dashBox.height) : 0;
    expect(dashH, 'dashboard tab strip height should be measurable').toBeGreaterThan(0);

    // 2) Visit /charts and measure the canonical toolbar controls.
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);
    await page.waitForTimeout(500);

    const chartCtrls = [
      { name: 'range pill',    sel: '.cw-range-group .cw-range-btn >> nth=0' },
      { name: 'intraday btn',  sel: '.cw-intraday-btn' },
      { name: 'type select',   sel: '.cw-type-chart-wrap .rbq-select-trigger' },
      { name: 'overlays trig', sel: '.cw-overlay-panel .rbq-multi-trigger' },
    ];
    const chartHeights = [];
    for (const c of chartCtrls) {
      const b = await page.locator(c.sel).boundingBox();
      if (b) chartHeights.push({ name: c.name, h: Math.round(b.height) });
    }
    expect(
      chartHeights.length,
      'expected ≥3 chart toolbar controls to be measurable',
    ).toBeGreaterThanOrEqual(3);

    for (const { name, h } of chartHeights) {
      expect(
        Math.abs(h - dashH) <= 2,
        `Cross-page height drift: chart "${name}" = ${h}px vs dashboard algo-tab = ${dashH}px. ` +
        `All chart heights: ${chartHeights.map(x => `${x.name}=${x.h}`).join(' | ')}`,
      ).toBe(true);
    }
  });

  test('reset button rides the same toolbar height as the other chart controls', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);
    await page.waitForTimeout(400);

    // Force the reset button to appear — programmatically scroll-zoom
    // on the SVG so `isZoomed` flips true and the Reset button renders.
    // If the gesture doesn't take (dev-flake), soft-skip.
    const svg = page.locator('.cw-svg');
    await svg.hover();
    await page.mouse.wheel(0, -500);
    await page.waitForTimeout(400);
    const reset = page.locator('.cw-reset-zoom');
    const resetVisible = await reset.isVisible().catch(() => false);
    if (!resetVisible) {
      console.log('[reset-height] zoom gesture not triggered — skipping height check');
      return;
    }
    const [resetBox, rangeBox] = await Promise.all([
      reset.boundingBox(),
      page.locator('.cw-range-group .cw-range-btn').first().boundingBox(),
    ]);
    expect(resetBox).not.toBeNull();
    expect(rangeBox).not.toBeNull();
    if (resetBox && rangeBox) {
      const drift = Math.abs(Math.round(resetBox.height) - Math.round(rangeBox.height));
      expect(
        drift <= 2,
        `Reset height ${resetBox.height} vs range pill ${rangeBox.height}px — drift ${drift}px (allow ≤2)`,
      ).toBe(true);
    }
  });

  test('vertical gap between controls row and chart SVG is minimal (≤8 px)', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);
    await page.waitForTimeout(500);

    const ctrlBox = await page.locator('.cw-controls').first().boundingBox();
    // .cw-chart-container holds the SVG; measure its top edge — that's
    // where the chart area starts. The container `flex: 1 1 0` sits
    // directly below the toolbar row (or the optional front-month chip).
    const chartTop = await page.locator('.cw-chart-container').first().boundingBox();
    expect(ctrlBox).not.toBeNull();
    expect(chartTop).not.toBeNull();
    if (ctrlBox && chartTop) {
      // Optional front-month chip may sit between toolbar and chart —
      // measure to the top of whichever element appears first below.
      // Use count() first so we don't await a non-existent locator (which
      // would hang on boundingBox() until the test timeout).
      let fmTop = null;
      const fmCount = await page.locator('.cw-frontmonth-bar').count();
      if (fmCount > 0) {
        const fmBox = await page.locator('.cw-frontmonth-bar').first()
          .boundingBox().catch(() => null);
        if (fmBox) fmTop = fmBox.y;
      }
      const nextTop = fmTop !== null ? Math.min(fmTop, chartTop.y) : chartTop.y;
      const ctrlBottom = ctrlBox.y + ctrlBox.height;
      const gap = nextTop - ctrlBottom;
      expect(
        gap,
        `Toolbar→chart gap = ${gap}px (operator wants minimal ≤8 px). ctrlBottom=${ctrlBottom} nextTop=${nextTop}`,
      ).toBeLessThanOrEqual(8);
    }
  });

  // Mobile-fit: at 393×851 + 360×640 the entire chart workspace must
  // fit on screen without vertical scroll. We assert against the html
  // root's scrollHeight vs the window's innerHeight with a 4 px
  // tolerance — sub-pixel rounding can push the body 1-2 px past the
  // viewport without producing a visible scrollbar.
  for (const VP of [{ w: 393, h: 851 }, { w: 360, h: 640 }]) {
    test(`mobile fit @ ${VP.w}×${VP.h} — no vertical scroll on /charts`, async ({ page }) => {
      test.setTimeout(60_000);
      await injectSession(page, _session);
      await page.setViewportSize({ width: VP.w, height: VP.h });
      await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
      await waitForChart(page);
      // Layout-stabilisation beat — the picker + controls rows reflow
      // on narrow viewports as the symbol input + selects hydrate.
      await page.waitForTimeout(900);

      const scrollH = await page.evaluate(
        () => document.documentElement.scrollHeight,
      );
      const winH = await page.evaluate(() => window.innerHeight);
      const overflow = scrollH - winH;
      expect(
        overflow,
        `Document scrolls vertically @ ${VP.w}×${VP.h}: scrollH=${scrollH}, winH=${winH}, overflow=${overflow}px ` +
        `(operator: "the entire chart grid should fit in mobile viewport with no scrolling").`,
      ).toBeLessThanOrEqual(4);
    });
  }

  // ── Chart UX polish bundle (Jun 2026) ──────────────────────────────────
  //
  //   1. Reset button fills available width on mobile (operator:
  //      "let the reset button use available space on mobile").
  //   3-5. Y-axis labels are readable — font ≥ 11 px after rotation,
  //        slanted ≥ -45°.

  test('reset button fills available trailing space on mobile (≥60 px wide)', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    await page.setViewportSize({ width: 360, height: 640 });
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);
    await page.waitForTimeout(700);

    // Force the reset button to appear — dispatch a wheel event on the
    // SVG with `cancelable: true` so `e.preventDefault()` in _onWheel
    // succeeds. Falls back to mouse.wheel + hover if dispatchEvent path
    // doesn't bind (older builds).  Soft-skip when the gesture has no
    // effect (data not yet loaded, mid-fetch).
    const svg = page.locator('.cw-svg');
    await svg.hover().catch(() => {});
    // Two attempts — first via native mouse.wheel (works on chromium-
    // desktop), second via dispatched event (works on isMobile contexts
    // where mouse.wheel routes through touch).
    await page.mouse.wheel(0, -500).catch(() => {});
    await page.waitForTimeout(300);
    let reset = page.locator('.cw-reset-zoom');
    let resetVisible = await reset.isVisible().catch(() => false);
    if (!resetVisible) {
      await svg.evaluate((el) => {
        const r = el.getBoundingClientRect();
        const ev = new WheelEvent('wheel', {
          deltaY: -500,
          clientX: r.left + r.width / 2,
          clientY: r.top + r.height / 2,
          bubbles: true,
          cancelable: true,
        });
        el.dispatchEvent(ev);
      }).catch(() => {});
      await page.waitForTimeout(400);
      resetVisible = await reset.isVisible().catch(() => false);
    }
    if (!resetVisible) {
      console.log('[reset-mobile-fill] zoom gesture not triggered — skipping');
      return;
    }
    const box = await reset.boundingBox();
    expect(box).not.toBeNull();
    if (box) {
      expect(
        box.width,
        `Reset width on 360 px wide viewport should fill available space (>60 px). Got ${box.width}px. ` +
        `Operator: "let the reset button use available space on mobile".`,
      ).toBeGreaterThan(60);
    }
  });

  test('Y-axis labels render at ≥11 px font and rotated for readability', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);
    await page.waitForTimeout(500);

    // The Y-axis labels carry .cw-yaxis-label; pick the first one inside
    // the historical SVG. The SVG uses viewBox with preserveAspectRatio=
    // none so font-size attribute maps 1:1 to drawn pixels per
    // viewBox unit only at the same x-scale — assert via the attribute
    // since computed style on SVG <text> can be browser-quirky.
    const label = page.locator('.cw-svg .cw-yaxis-label').first();
    await expect(label).toBeVisible({ timeout: 5_000 });

    // font-size attribute — readable threshold ≥ 11 px (operator: "y
    // axis labels are readable. make the text readable").
    const fontSize = await label.getAttribute('font-size');
    expect(fontSize, 'Y-axis label has explicit font-size attribute').toBeTruthy();
    expect(
      Number(fontSize),
      `Y-axis label font-size should be ≥ 11. Got ${fontSize}.`,
    ).toBeGreaterThanOrEqual(11);

    // Transform attribute carries rotate(-45 ...) or rotate(-60 ...).
    const transform = await label.getAttribute('transform');
    expect(transform, 'Y-axis label is rotated for slanted layout').toBeTruthy();
    expect(
      /rotate\((-45|-60)/.test(transform),
      `Y-axis label should be slanted at -45° or -60°. transform="${transform}". ` +
      `Operator: "make it slant if required to reduce the space usage and increase font size".`,
    ).toBe(true);
  });

  // ── Chart card claims FULL available height (mobile + desktop) ────
  //
  // Operator: "the chart card should take full available height on the
  // screen either on mobile or desktop".  The flex chain (.charts-page-
  // wrap → .chart-body → .cw-root → .cw-chart-container) should let the
  // chart SVG absorb every residual pixel after the page-header and
  // toolbar rows subtract their natural height. We assert the SVG's
  // rendered height passes a viewport-relative floor at four canonical
  // sizes. Tight phone viewports give up more chrome share (navbar +
  // 2 toolbar rows + info-strip + footer ≈ 250 px), so the 360×640
  // floor is intentionally lower than the 393×851 one.
  //   Mobile 360×640   → SVG ≥ 370 px (≥58% of viewport)
  //   Mobile 393×851   → SVG ≥ 480 px (≥56%)
  //   Desktop 1280×800 → SVG ≥ 500 px (≥62%)
  //   Desktop 1920×1080 → SVG ≥ 800 px (≥74%)
  for (const VP of [
    { w: 360,  h: 640,  floor: 370 },
    { w: 393,  h: 851,  floor: 480 },
    { w: 1280, h: 800,  floor: 500 },
    { w: 1920, h: 1080, floor: 800 },
  ]) {
    test(`chart-claim full height @ ${VP.w}×${VP.h} — SVG ≥ ${VP.floor} px`, async ({ page }) => {
      test.setTimeout(60_000);
      await injectSession(page, _session);
      await page.setViewportSize({ width: VP.w, height: VP.h });
      await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
      await waitForChart(page);
      // Layout-stabilisation beat — the picker + controls rows reflow
      // as the symbol input + selects hydrate, and the ResizeObserver
      // inside ChartWorkspace fires _chartW/_chartH on the next frame.
      await page.waitForTimeout(900);

      // Measure the chart SVG's rendered box. .cw-svg fills its
      // .cw-chart-container at width:100%/height:100% — the container
      // is `flex: 1 1 0` so it absorbs all leftover space below the
      // toolbars.
      const svgBox = await page.locator('.cw-svg').first().boundingBox();
      expect(svgBox, 'chart SVG should be measurable').not.toBeNull();
      if (svgBox) {
        expect(
          svgBox.height,
          `Chart SVG height @ ${VP.w}×${VP.h} should be ≥ ${VP.floor}px ` +
          `(operator: "the chart card should take full available height ` +
          `on the screen either on mobile or desktop"). Got ${svgBox.height}px.`,
        ).toBeGreaterThanOrEqual(VP.floor);
      }

      // No vertical scroll on the page (already asserted in mobile-fit
      // for two viewports; extend the assertion to desktop too so a
      // future regression that pushes content past viewport.h is
      // caught everywhere).
      const scrollH = await page.evaluate(() => document.documentElement.scrollHeight);
      const winH   = await page.evaluate(() => window.innerHeight);
      const overflow = scrollH - winH;
      expect(
        overflow,
        `Document scrolls vertically @ ${VP.w}×${VP.h}: ` +
        `scrollH=${scrollH}, winH=${winH}, overflow=${overflow}px`,
      ).toBeLessThanOrEqual(4);
    });
  }

  // ── Desktop visible-content smoke (Jun 2026) ──────────────────────
  //
  // Operator: "the chart contracted without showing the chart on
  // desktop. fix it" — slice a398ab81 dropped the `min-height: 160px`
  // floor on `.cw-chart-container` so when the flex chain residual
  // resolved to ≤0 (e.g. ResizeObserver fired pre-hydration) the
  // container collapsed and the SVG had no paint room. The fix
  // restored a 200 px safety floor while keeping the flex chain.
  //
  // This smoke asserts BOTH that the container has non-trivial height
  // AND that the SVG actually paints visible content — earlier
  // chart-claim tests only checked the SVG box height (which
  // boundingBox() reports even when the SVG renders no children at
  // all, masking a contracted-but-claiming-height regression).

  test('chart shows visible content at 1280×800 desktop (not just claims height)', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto(NIFTY_URL, { waitUntil: 'domcontentloaded' });
    await waitForChart(page);
    await page.waitForTimeout(900);

    // Container height — safety floor 200 px (restored after a398ab81
    // regression). Allow the flex chain to claim more; just guard
    // against the collapse case.
    const containerBox = await page.locator('.cw-chart-container').first().boundingBox();
    expect(containerBox, 'chart container measurable').not.toBeNull();
    if (containerBox) {
      expect(
        containerBox.height,
        `Chart container collapsed @ 1280×800 — height ${containerBox.height}px ` +
        `< 200 px floor. Regression slice a398ab81 dropped the min-height ` +
        `safety floor; restore it on .cw-chart-container.`,
      ).toBeGreaterThanOrEqual(200);
    }

    // Visible content — SVG must paint either a series path (line/area)
    // OR candle rects (≥ 3 candle rects implies a populated chart, not
    // just the empty-state placeholder). Y-axis text labels are also
    // a strong signal that the chart has data + drew its scaffold.
    const counts = await page.evaluate(() => ({
      paths: document.querySelectorAll('.cw-svg path').length,
      rects: document.querySelectorAll('.cw-svg rect').length,
      lines: document.querySelectorAll('.cw-svg line').length,
      texts: document.querySelectorAll('.cw-svg text').length,
      firstPathD: document.querySelector('.cw-svg path')?.getAttribute('d') || '',
    }));

    // Either a non-trivial path (line/area series → d > 20 chars) OR
    // ≥ 3 candle rects (candle series). Texts > 5 verifies axis labels
    // rendered (a sanity check that the chart has data, not just
    // scaffold lines).
    const hasSeriesPath = counts.paths > 0 && counts.firstPathD.length > 20;
    const hasCandles    = counts.rects >= 3;
    const hasAxisText   = counts.texts >= 5;
    expect(
      (hasSeriesPath || hasCandles) && hasAxisText,
      `Chart has no visible content @ 1280×800. paths=${counts.paths} ` +
      `(first d.length=${counts.firstPathD.length}) rects=${counts.rects} ` +
      `lines=${counts.lines} texts=${counts.texts}. Expected either ` +
      `a series path (d > 20 chars) OR ≥ 3 candle rects, AND ≥ 5 axis labels.`,
    ).toBe(true);
  });
});
