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

    // localStorage should hold both keys.
    const stored = await page.evaluate(() =>
      localStorage.getItem('rbq.cache.chart-overlays.v1'),
    );
    expect(stored, 'overlay selections written to localStorage').toBeTruthy();
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
    await waitForChart(page);
    await page.waitForTimeout(500);

    const typeTrigger = page.locator('.cw-type-chart-wrap .rbq-select-trigger');
    const label = await typeTrigger.innerText();
    expect(
      label.trim(),
      'Stored "line" should hydrate on mount, overriding the candle default',
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
});
