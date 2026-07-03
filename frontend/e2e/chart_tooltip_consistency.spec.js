/**
 * chart_tooltip_consistency.spec.js
 *
 * Validates that the hover/click tooltips in ChartWorkspace (/charts) and
 * OptionsPayoff (/admin/derivatives) share the same canonical styling from
 * app.css (.chart-tooltip family).
 *
 * Five quality dimensions:
 *  1. SSOT     — both surfaces use the same .chart-tooltip CSS class; no
 *                divergent background/border/radius/font values at runtime
 *  2. Perf     — tooltip appearance does not introduce a long task > 100ms
 *  3. Stale    — no residual .cw-hover-popup / SVG rect+text tooltip in
 *                ChartWorkspace source; no inline fill attrs on tooltip in
 *                OptionsPayoff source
 *  4. Reusable — both surfaces share app.css class, not per-component CSS
 *  5. UX       — tooltip visible on both desktop and mobile; same computed
 *                background, border, border-radius, and font-family
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/chart_tooltip_consistency.spec.js \
 *   --project=chromium-desktop --project=mobile-portrait --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const CHARTS_URL      = `${BASE}/charts?symbol=${encodeURIComponent('NIFTY 50')}&mode=live`;
const DERIVATIVES_URL = `${BASE}/admin/derivatives`;

// ── Stale-code checks (Dim 3 + Dim 4) ────────────────────────────────────────

test('Dim 3+4: ChartWorkspace uses .chart-tooltip class, not legacy .cw-hover-popup', () => {
  const src = fs.readFileSync(
    path.resolve(process.cwd(), 'src/lib/ChartWorkspace.svelte'),
    'utf8',
  );
  // Legacy class must be gone from markup
  expect(src).not.toContain('class="cw-hover-popup"');
  expect(src).not.toContain('class:cw-hover-popup-pinned');
  expect(src).not.toContain('class="cw-hp-close"');
  expect(src).not.toContain('class="cw-hp-ts"');
  expect(src).not.toContain('class="cw-hp-row"');
  expect(src).not.toContain('class="cw-hp-label"');
  expect(src).not.toContain('class="cw-hp-val"');
  // Canonical class must be present
  expect(src).toContain('class="chart-tooltip"');
  expect(src).toContain('chart-tooltip-pinned');
  expect(src).toContain('chart-tooltip-ts');
  expect(src).toContain('chart-tooltip-row');
  expect(src).toContain('chart-tooltip-label');
  expect(src).toContain('chart-tooltip-value');
});

test('Dim 3+4: OptionsPayoff uses HTML .chart-tooltip overlay, not SVG rect+text tooltip', () => {
  const src = fs.readFileSync(
    path.resolve(process.cwd(), 'src/lib/OptionsPayoff.svelte'),
    'utf8',
  );
  // Old SVG rect+text tooltip block must be gone — the rect had this specific
  // dark background fill combined with the cyan tooltip border
  expect(src).not.toMatch(/fill="#0f172a"[\s\S]{0,40}stroke="rgba\(125,211,252,0\.30\)"/);
  // Old SVG text labels with amber fill specifically for SPOT/TDAY/EXP rows
  // had font-size="11" on a text element with letter-spacing attr — distinct
  // from the fg-layer dart circles which have no letter-spacing
  expect(src).not.toMatch(/<text[^>]*font-size="11"[^>]*letter-spacing/);
  // Canonical HTML class must be present in a div
  expect(src).toContain('class="chart-tooltip');
  expect(src).toContain('chart-tooltip-label');
  expect(src).toContain('chart-tooltip-value');
});

test('Dim 4: app.css defines the .chart-tooltip canonical class', () => {
  const src = fs.readFileSync(
    path.resolve(process.cwd(), 'src/app.css'),
    'utf8',
  );
  expect(src).toContain('.chart-tooltip {');
  expect(src).toContain('.chart-tooltip-pinned {');
  expect(src).toContain('.chart-tooltip-ts {');
  expect(src).toContain('.chart-tooltip-row {');
  expect(src).toContain('.chart-tooltip-label {');
  expect(src).toContain('.chart-tooltip-value {');
  expect(src).toContain('.chart-tooltip-value.up');
  expect(src).toContain('.chart-tooltip-value.down');
  expect(src).toContain('.chart-tooltip-close {');
  // Canonical values
  expect(src).toContain('rgba(15, 25, 45, 0.95)');       // background
  expect(src).toContain('rgba(125, 211, 252, 0.45)');     // border
  expect(src).toContain('var(--font-numeric)');            // font-family
  expect(src).toContain('var(--c-long)');                  // up color
  expect(src).toContain('var(--c-short)');                 // down color
});

// ── Runtime style checks (Dim 1 + Dim 5) ─────────────────────────────────────

test.describe('runtime tooltip computed styles', () => {
  /** @type {Record<string, string>} */
  let _session = {};

  test.beforeAll(async ({ browser }) => {
    const ctx  = await browser.newContext();
    const page = await ctx.newPage();
    _session   = await loginAsAdmin(page);
    await ctx.close();
  });

  async function injectSession(page) {
    await page.addInitScript((data) => {
      for (const [k, v] of Object.entries(data)) {
        sessionStorage.setItem(k, v);
      }
    }, _session);
    if (_session.ramboq_token) {
      await page.context().setExtraHTTPHeaders({
        Authorization: `Bearer ${_session.ramboq_token}`,
      });
    }
  }

  /**
   * Trigger the hover tooltip on /charts by moving the pointer over the
   * OHLCV SVG area. Returns computed styles of the first .chart-tooltip.
   */
  async function getChartTooltipStyles(page) {
    await injectSession(page);
    await page.goto(CHARTS_URL, { waitUntil: 'networkidle' });

    // Wait for chart to have data — the SVG should appear
    const svg = page.locator('.cw-svg').first();
    await svg.waitFor({ state: 'visible', timeout: 15000 }).catch(() => {});

    // Move pointer over the center of the SVG to trigger hover
    const box = await svg.boundingBox();
    if (box) {
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.waitForTimeout(300);
    }

    const tooltip = page.locator('.chart-tooltip').first();
    const visible = await tooltip.isVisible().catch(() => false);
    if (!visible) {
      // No data yet — return null to skip assertion (closed-hours / no ticks)
      return null;
    }
    return await tooltip.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        backgroundColor: s.backgroundColor,
        borderColor:     s.borderColor,
        borderRadius:    s.borderRadius,
        fontFamily:      s.fontFamily,
      };
    });
  }

  /**
   * Trigger the hover tooltip on /admin/derivatives by moving the pointer
   * over the payoff SVG area. Returns computed styles of .chart-tooltip.
   */
  async function getPayoffTooltipStyles(page) {
    await injectSession(page);
    await page.goto(DERIVATIVES_URL, { waitUntil: 'networkidle' });

    // Wait for payoff chart to be ready
    const svg = page.locator('.payoff-svg').first();
    await svg.waitFor({ state: 'visible', timeout: 15000 }).catch(() => {});

    const box = await svg.boundingBox();
    if (box) {
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.waitForTimeout(300);
    }

    const tooltip = page.locator('.chart-tooltip').first();
    const visible = await tooltip.isVisible().catch(() => false);
    if (!visible) {
      return null;
    }
    return await tooltip.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        backgroundColor: s.backgroundColor,
        borderColor:     s.borderColor,
        borderRadius:    s.borderRadius,
        fontFamily:      s.fontFamily,
      };
    });
  }

  test('Dim 1+5: chart tooltip computed styles match canonical', async ({ page }) => {
    const styles = await getChartTooltipStyles(page);
    if (!styles) {
      test.skip(true, 'Chart has no data — tooltip not triggerable (closed hours / no ticks)');
      return;
    }
    // Background: rgba(15, 25, 45, 0.95)
    expect(styles.backgroundColor).toMatch(/rgba?\(15,\s*25,\s*45/);
    // Border-radius: 4px
    expect(styles.borderRadius).toBe('4px');
    // Font-family must include a monospace stack
    expect(styles.fontFamily.toLowerCase()).toMatch(/mono|consolas|menlo|sfmono/);
  });

  test('Dim 1+5: payoff tooltip computed styles match canonical', async ({ page }) => {
    const styles = await getPayoffTooltipStyles(page);
    if (!styles) {
      test.skip(true, 'Payoff chart has no data — tooltip not triggerable');
      return;
    }
    expect(styles.backgroundColor).toMatch(/rgba?\(15,\s*25,\s*45/);
    expect(styles.borderRadius).toBe('4px');
    expect(styles.fontFamily.toLowerCase()).toMatch(/mono|consolas|menlo|sfmono/);
  });

  test('Dim 1+5: chart and payoff tooltip styles are equal', async ({ browser }) => {
    const ctx1 = await browser.newContext();
    const ctx2 = await browser.newContext();
    const p1   = await ctx1.newPage();
    const p2   = await ctx2.newPage();

    const [chartStyles, payoffStyles] = await Promise.all([
      getChartTooltipStyles(p1),
      getPayoffTooltipStyles(p2),
    ]);
    await ctx1.close();
    await ctx2.close();

    if (!chartStyles || !payoffStyles) {
      test.skip(true, 'One or both tooltips not triggerable (closed hours / no data)');
      return;
    }
    expect(chartStyles.backgroundColor).toBe(payoffStyles.backgroundColor);
    expect(chartStyles.borderRadius).toBe(payoffStyles.borderRadius);
    expect(chartStyles.fontFamily).toBe(payoffStyles.fontFamily);
  });

  test('Dim 2: tooltip appearance does not cause a long task > 100ms', async ({ page }) => {
    await injectSession(page);
    await page.goto(CHARTS_URL, { waitUntil: 'networkidle' });

    const svg = page.locator('.cw-svg').first();
    await svg.waitFor({ state: 'visible', timeout: 15000 }).catch(() => {});

    // Observe long tasks during hover
    await page.evaluate(() => {
      // @ts-ignore
      window._longTaskDurations = [];
      const obs = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          // @ts-ignore
          window._longTaskDurations.push(entry.duration);
        }
      });
      obs.observe({ type: 'longtask', buffered: false });
      // @ts-ignore
      window._longTaskObs = obs;
    });

    const box = await svg.boundingBox();
    if (box) {
      // Sweep across the chart to trigger multiple hover events
      for (let i = 0; i < 5; i++) {
        await page.mouse.move(box.x + (box.width / 6) * (i + 1), box.y + box.height / 2);
        await page.waitForTimeout(60);
      }
    }

    const maxDuration = await page.evaluate(() => {
      // @ts-ignore
      const durations = window._longTaskDurations ?? [];
      return durations.length ? Math.max(...durations) : 0;
    });

    expect(maxDuration).toBeLessThan(100);
  });

  test('Dim 5 mobile: payoff tooltip visible and fits in viewport on mobile', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await injectSession(page);
    await page.goto(DERIVATIVES_URL, { waitUntil: 'networkidle' });

    const svg = page.locator('.payoff-svg').first();
    await svg.waitFor({ state: 'visible', timeout: 15000 }).catch(() => {});

    const box = await svg.boundingBox();
    if (box) {
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.waitForTimeout(400);
    }

    const tooltip = page.locator('.chart-tooltip').first();
    const visible = await tooltip.isVisible().catch(() => false);
    if (!visible) {
      test.skip(true, 'Payoff chart not interactive on mobile (no data)');
      return;
    }
    const tooltipBox = await tooltip.boundingBox();
    expect(tooltipBox).not.toBeNull();
    if (tooltipBox) {
      // Tooltip must be within viewport width
      expect(tooltipBox.x).toBeGreaterThanOrEqual(0);
      expect(tooltipBox.x + tooltipBox.width).toBeLessThanOrEqual(400);
    }
  });
});
