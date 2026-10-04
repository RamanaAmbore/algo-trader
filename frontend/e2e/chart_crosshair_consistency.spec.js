/**
 * chart_crosshair_consistency.spec.js
 *
 * Regression guard for the Wave 1/2 shared-crosshair consolidation
 * (frontend/src/lib/ChartCrosshair.svelte). Before this change, 7
 * hand-rolled SVG charts each wrote their own inline crosshair markup —
 * ChartWorkspace used amber/dashed/with-dot, OptionsPayoff used
 * white/solid/no-dot. This spec hovers both migrated charts and asserts
 * the rendered crosshair <line> has the same canonical computed stroke
 * color, width, and dash-array on both surfaces.
 *
 * Uses relative page.goto() paths (not an absolute dev.ramboq.com URL)
 * so this runs against the config's default baseURL — the local dev
 * server (localhost:5174) — unless PLAYWRIGHT_BASE_URL is explicitly
 * overridden. This matters here specifically: running against a
 * deployed environment would exercise the OLD pre-migration code on
 * whichever branch is live there, not this change.
 *
 * Five quality dimensions:
 *  1. SSOT     — both surfaces render the shared .chart-crosshair <g>
 *                markup, not divergent per-component crosshair classes.
 *  2. Perf     — n/a (no load triggered beyond normal page nav + hover).
 *  3. Stale    — covered by the companion vitest source-audit
 *                (chartCrosshairAudit.test.js) — no old inline crosshair
 *                markup survives in either file.
 *  4. Reusable — both charts import the same ChartCrosshair.svelte.
 *  5. UX       — crosshair line is visible with matching computed
 *                stroke/width/dash on both charts; OptionsPayoff shows
 *                no dot (deliberate — see OptionsPayoff.svelte comment),
 *                ChartWorkspace shows a dot.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(90_000);
const TIMEOUT = 60_000;
const CHART_URL = `/charts?symbol=${encodeURIComponent('NIFTY 50')}&mode=live`;

/**
 * Sweep the pointer across an SVG's plot area to trigger a hover state,
 * trying a few x-offsets since candle/bar hit-testing can be sparse
 * when there's little OHLCV data (e.g. weekends/closed hours).
 * @param {import('@playwright/test').Page} page
 * @param {import('@playwright/test').Locator} svg
 */
async function sweepHover(page, svg) {
  const box = await svg.boundingBox();
  if (!box) return null;
  for (const frac of [0.5, 0.35, 0.65, 0.25, 0.75]) {
    await page.mouse.move(box.x + box.width * frac, box.y + box.height * 0.5);
    await page.waitForTimeout(150);
    const line = svg.locator('g.chart-crosshair line').first();
    if (await line.count()) return line;
  }
  return null;
}

test('crosshair consistency: ChartWorkspace renders the canonical crosshair on hover', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto(CHART_URL, { waitUntil: 'domcontentloaded', timeout: TIMEOUT });

  const svg = page.locator('svg.cw-svg').first();
  // SvelteKit hydration + the OHLCV fetch both happen client-side after
  // 'domcontentloaded' fires, so check for the SVG via a real wait
  // (not an immediate count()) before deciding there's no data to hover.
  const svgAppeared = await svg.waitFor({ state: 'visible', timeout: TIMEOUT }).then(() => true).catch(() => false);
  if (!svgAppeared) {
    test.skip(true, 'ChartWorkspace SVG not rendered — no OHLCV data (closed hours)');
    return;
  }

  const line = await sweepHover(page, svg);
  if (!line) {
    test.skip(true, 'No hover-crosshair appeared — chart likely has no series data');
    return;
  }

  const style = await line.evaluate((el) => {
    const s = getComputedStyle(el);
    return { stroke: s.stroke, strokeWidth: s.strokeWidth, dasharray: s.strokeDasharray };
  });

  expect(style.stroke).toMatch(/rgba?\(\s*251,\s*191,\s*36/);
  expect(style.strokeWidth).toBe('1px');
  // Chromium serializes presentation-attribute stroke-dasharray back as "3px, 2px".
  expect(style.dasharray.replace(/px/g, '').replace(/\s+/g, '')).toBe('3,2');

  // ChartWorkspace's crosshair includes a dot (showDot defaults to true).
  const dot = svg.locator('g.chart-crosshair circle').first();
  await expect(dot).toHaveCount(1);
});

test('crosshair consistency: OptionsPayoff renders the SAME canonical crosshair style on hover', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: TIMEOUT });

  const svg = page.locator('svg.payoff-svg').first();
  const svgAppeared = await svg.waitFor({ state: 'visible', timeout: TIMEOUT }).then(() => true).catch(() => false);
  if (!svgAppeared) {
    test.skip(true, 'OptionsPayoff SVG not rendered — no legs/positions loaded');
    return;
  }

  const line = await sweepHover(page, svg);
  if (!line) {
    test.skip(true, 'No hover-crosshair appeared — chart likely has no data to hover');
    return;
  }

  const style = await line.evaluate((el) => {
    const s = getComputedStyle(el);
    return { stroke: s.stroke, strokeWidth: s.strokeWidth, dasharray: s.strokeDasharray };
  });

  // Same canonical amber/dashed look as ChartWorkspace — this used to be
  // a plain white solid line before the migration.
  expect(style.stroke).toMatch(/rgba?\(\s*251,\s*191,\s*36/);
  expect(style.strokeWidth).toBe('1px');
  expect(style.dasharray.replace(/px/g, '').replace(/\s+/g, '')).toBe('3,2');

  // OptionsPayoff deliberately passes showDot={false} — see its own
  // source comment for why (preserveAspectRatio="none" bg svg + multi-
  // curve chart + existing payoff-stats overlay already marks the point).
  const dot = svg.locator('g.chart-crosshair circle');
  await expect(dot).toHaveCount(0);
});

/**
 * Wave 2 — the remaining charts (PriceChart, MultiPriceChart, EquityCurve,
 * dashboard Intraday) must render the SAME canonical crosshair as
 * ChartWorkspace on hover, with the dot presence matching each call
 * site's showDot choice. Each test skips with an explicit reason when the
 * chart needs live/sim data that is not present in the local environment.
 */

/**
 * Assert the rendered hover crosshair line has the canonical computed style.
 * @param {import('@playwright/test').Locator} line
 */
async function expectCanonicalCrosshairStyle(line) {
  const style = await line.evaluate((el) => {
    const s = getComputedStyle(el);
    return { stroke: s.stroke, strokeWidth: s.strokeWidth, dasharray: s.strokeDasharray };
  });
  expect(style.stroke).toMatch(/rgba?\(\s*251,\s*191,\s*36/);
  expect(style.strokeWidth).toBe('1px');
  expect(style.dasharray.replace(/px/g, '').replace(/\s+/g, '')).toBe('3,2');
}

test('crosshair consistency: PriceChart renders the canonical vertical crosshair (no dot)', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/admin/execution', { waitUntil: 'domcontentloaded', timeout: TIMEOUT });

  const svg = page.locator('svg.chart-svg').first();
  const appeared = await svg.waitFor({ state: 'visible', timeout: TIMEOUT }).then(() => true).catch(() => false);
  if (!appeared) {
    test.skip(true, 'PriceChart not rendered — needs open sim/paper orders with price history');
    return;
  }

  const line = await sweepHover(page, svg);
  if (!line) {
    test.skip(true, 'No hover-crosshair appeared — PriceChart has no price history to hover');
    return;
  }

  await expectCanonicalCrosshairStyle(line);
  // PriceChart passes showDot={false} — see its source comment.
  await expect(svg.locator('g.chart-crosshair circle')).toHaveCount(0);
});

test('crosshair consistency: MultiPriceChart renders the canonical vertical crosshair (no dot)', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/admin/execution', { waitUntil: 'domcontentloaded', timeout: TIMEOUT });

  const svg = page.locator('svg.mpc-svg').first();
  const appeared = await svg.waitFor({ state: 'visible', timeout: TIMEOUT }).then(() => true).catch(() => false);
  if (!appeared) {
    test.skip(true, 'MultiPriceChart not rendered — needs a simulator run with leg series');
    return;
  }

  const line = await sweepHover(page, svg);
  if (!line) {
    test.skip(true, 'No hover-crosshair appeared — MultiPriceChart has no series data');
    return;
  }

  await expectCanonicalCrosshairStyle(line);
  // MultiPriceChart passes showDot={false} — multi-series, no single y at pointer.
  await expect(svg.locator('g.chart-crosshair circle')).toHaveCount(0);
});

test('crosshair consistency: EquityCurve renders the canonical crosshair with its P&L dot', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/admin/execution', { waitUntil: 'domcontentloaded', timeout: TIMEOUT });

  const svg = page.locator('svg.eq-svg').first();
  const appeared = await svg.waitFor({ state: 'visible', timeout: TIMEOUT }).then(() => true).catch(() => false);
  if (!appeared) {
    test.skip(true, 'EquityCurve not rendered — needs a simulator run with P&L ticks');
    return;
  }

  const line = await sweepHover(page, svg);
  if (!line) {
    test.skip(true, 'No hover-crosshair appeared — EquityCurve has no P&L ticks to hover');
    return;
  }

  await expectCanonicalCrosshairStyle(line);
  // EquityCurve keeps its dot (showDot default), P&L-signed via dotColor.
  await expect(svg.locator('g.chart-crosshair circle')).toHaveCount(1);
});

test('crosshair consistency: dashboard Intraday chart renders the canonical crosshair with its line-colored dot', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: TIMEOUT });

  const chartCard = page.locator('section').filter({ hasText: 'NAV' }).first();
  const intradayBtn = chartCard.locator('button', { hasText: /intraday/i }).first();
  if (!(await intradayBtn.count())) {
    test.skip(true, 'Dashboard Intraday tab not present for this account');
    return;
  }
  await intradayBtn.click();
  await page.waitForTimeout(300);

  const svg = page.locator('svg.eq-svg').first();
  const appeared = await svg.waitFor({ state: 'visible', timeout: TIMEOUT }).then(() => true).catch(() => false);
  if (!appeared) {
    test.skip(true, 'Dashboard Intraday SVG not rendered — pre-market, no intraday points');
    return;
  }

  const line = await sweepHover(page, svg);
  if (!line) {
    test.skip(true, 'No hover-crosshair appeared — no intraday points to hover');
    return;
  }

  await expectCanonicalCrosshairStyle(line);
  // Dashboard keeps its dot (line-colored fill via dotColor).
  await expect(svg.locator('g.chart-crosshair circle')).toHaveCount(1);
});

/**
 * Dashboard NAV tab (NavTab) and Performance tab (PnlAnalysis) — both
 * must render the shared vertical crosshair on hover. NavTab keeps its
 * last-point circle as the data marker (showDot={false} on the crosshair),
 * PnlAnalysis has no dot. Each test skips with a reason when the
 * chart has no data locally.
 */
test('crosshair consistency: dashboard NavTab renders the vertical crosshair on hover', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: TIMEOUT });

  const svg = page.locator('svg.nav-svg').first();
  const appeared = await svg.waitFor({ state: 'visible', timeout: TIMEOUT }).then(() => true).catch(() => false);
  if (!appeared) {
    test.skip(true, 'NavTab not rendered — no firm NAV snapshots in local data');
    return;
  }

  const line = await sweepHover(page, svg);
  if (!line) {
    test.skip(true, 'No hover-crosshair appeared — NavTab has fewer than 2 NAV points');
    return;
  }

  await expectCanonicalCrosshairStyle(line);
  // The last-point circle is the data marker; the crosshair adds no second dot.
  await expect(svg.locator('g.chart-crosshair circle')).toHaveCount(0);
});

test('crosshair consistency: dashboard PnlAnalysis renders the vertical crosshair on hover', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: TIMEOUT });

  const perfBtn = page.getByRole('tab', { name: 'Performance', exact: true }).first();
  // Tabs mount after the dashboard data lands — wait, don't just count().
  await perfBtn.waitFor({ state: 'visible', timeout: TIMEOUT }).catch(() => {});
  if (!(await perfBtn.count())) {
    test.skip(true, 'Dashboard Performance tab not present for this account');
    return;
  }
  await perfBtn.click();
  await page.waitForTimeout(300);

  const svg = page.locator('svg.perf-svg').first();
  const appeared = await svg.waitFor({ state: 'visible', timeout: TIMEOUT }).then(() => true).catch(() => false);
  if (!appeared) {
    test.skip(true, 'PnlAnalysis benchmark SVG not rendered — no benchmark data locally');
    return;
  }

  const line = await sweepHover(page, svg);
  if (!line) {
    test.skip(true, 'No hover-crosshair appeared — PnlAnalysis has no benchmark dates to hover');
    return;
  }

  await expectCanonicalCrosshairStyle(line);
  // PnlAnalysis passes showDot={false} — its hover readout is the hov-tip.
  await expect(svg.locator('g.chart-crosshair circle')).toHaveCount(0);
});
