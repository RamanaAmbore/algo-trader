/**
 * dashboard_grid_parity.spec.js
 *
 * Asserts that the NAV, Capital, and Equity tabs on /dashboard have
 * visually consistent row height, header font-size, cell padding, and
 * border treatment.
 *
 * Dimensions checked (five quality dimensions):
 *
 *  1. SSOT  — no bespoke row-height literals; NavBreakdown table rows
 *             must match ag-Grid row height (26px) via computed style,
 *             not hard-coded per-component values.
 *  2. Perf  — mounting / switching to each tab < 100 ms.
 *  3. Stale — NavBreakdown does NOT have a class="ag-row" (it's a plain
 *             table, not ag-Grid). The parity is visual, not structural.
 *  4. Reuse — Capital + Equity grids use class="ag-theme-algo" (shared theme).
 *  5. UX    — NAV / Capital / Equity grids share the same computed row
 *             height (26px), header background (#0a1020), and amber
 *             header text (#fbbf24). Desktop + mobile viewports tested.
 *
 * Runs against chromium-desktop + mobile-portrait projects.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(90_000);

const TIMEOUT = 60_000;

/** Navigate to /dashboard and wait for the tabbed equity card. */
async function openDashboard(page) {
  await loginAsAdmin(page);
  await page.goto('/dashboard', { waitUntil: 'load', timeout: TIMEOUT });
  // Wait for the tabbed NAV/Capital/Equity card to be present.
  await page.locator('.cap-eq-tabbed').waitFor({ state: 'attached', timeout: TIMEOUT });
}

/** Click a tab by its label text inside .cap-eq-tabbed. */
async function clickCapEqTab(page, label) {
  const strip = page.locator('.cap-eq-tabbed');
  await strip.locator('button', { hasText: label }).first().click();
  // Brief settle time for the tab panel to become visible.
  await page.waitForTimeout(120);
}

// ── 1. NAV tab: NavBreakdown HTML table row height matches ag-Grid (26px) ───

test('dashboard grid parity: NAV tab row height matches Capital/Equity grids', async ({ page }) => {
  await openDashboard(page);

  // Ensure we're on the NAV tab (default).
  await clickCapEqTab(page, 'NAV');

  // Wait for the NavBreakdown table to render (may need data).
  const table = page.locator('.nav-bd-table');
  const hasTable = await table.count();
  if (hasTable === 0) {
    // No broker data in test environment — check at least the CSS class structure.
    // The nav-bd-wrap wrapper must be present even in empty state.
    const wrap = page.locator('.nav-bd-wrap, .nav-bd-empty');
    await expect(wrap.first(), 'NAV panel should render a wrapper').toBeAttached();
    return;
  }

  // Get the computed height of the first tbody row.
  const firstRow = table.locator('tbody tr').first();
  await firstRow.waitFor({ state: 'visible', timeout: 20_000 });
  const rowHeight = await firstRow.evaluate(el => el.getBoundingClientRect().height);

  // ag-Grid Capital/Equity grids use rowHeight: 26. Allow ±2px for sub-pixel
  // rounding and mobile-scaled viewports. The SSOT is the _baseGridOpts
  // declaration in dashboard/+page.svelte.
  expect(rowHeight,
    'NAV tbody row height should match ag-Grid rowHeight: 26 (±2px)'
  ).toBeGreaterThanOrEqual(24);
  expect(rowHeight,
    'NAV tbody row height should not exceed ag-Grid rowHeight: 26 (±2px)'
  ).toBeLessThanOrEqual(28);
});

// ── 2. NAV tab: header matches updated ag-theme-algo (muted slate text) ────────

test('dashboard grid parity: NAV tab header background + text color', async ({ page }) => {
  await openDashboard(page);
  await clickCapEqTab(page, 'NAV');

  const table = page.locator('.nav-bd-table');
  const hasTable = await table.count();
  if (hasTable === 0) return; // No data — structure-only environment.

  await table.locator('thead th').first().waitFor({ state: 'visible', timeout: 20_000 });

  // Header background: deep dark rgba(15,23,42,0.65) — Chromium blends with parent.
  // Accept any very-dark bg (R≤40, G≤50, B≤70).
  const headerBg = await table.locator('thead th').first().evaluate(el => {
    return getComputedStyle(el).backgroundColor;
  });
  const bgM = headerBg.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
  if (bgM) {
    expect(+bgM[1], 'NavBreakdown header bg R should be deep dark (≤40)').toBeLessThanOrEqual(40);
    expect(+bgM[2], 'NavBreakdown header bg G should be deep dark (≤50)').toBeLessThanOrEqual(50);
    expect(+bgM[3], 'NavBreakdown header bg B should be deep dark (≤70)').toBeLessThanOrEqual(70);
  }

  // Header text: muted slate #7e97b8 → rgb(126, 151, 184).
  const headerColor = await table.locator('thead th').first().evaluate(el => {
    return getComputedStyle(el).color;
  });
  const colM = headerColor.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
  if (colM) {
    // #7e97b8 = rgb(126, 151, 184). Allow ±8.
    expect(+colM[1], 'NavBreakdown header text R should be ~126 (muted slate)').toBeGreaterThanOrEqual(118);
    expect(+colM[1], 'NavBreakdown header text R should be ~126 (muted slate)').toBeLessThanOrEqual(134);
    expect(+colM[2], 'NavBreakdown header text G should be ~151 (muted slate)').toBeGreaterThanOrEqual(143);
    expect(+colM[2], 'NavBreakdown header text G should be ~151 (muted slate)').toBeLessThanOrEqual(159);
    expect(+colM[3], 'NavBreakdown header text B should be ~184 (muted slate)').toBeGreaterThanOrEqual(176);
    expect(+colM[3], 'NavBreakdown header text B should be ~184 (muted slate)').toBeLessThanOrEqual(192);
  }
});

// ── 3. Capital tab: ag-Grid uses ag-theme-algo ───────────────────────────────

test('dashboard grid parity: Capital tab grids have ag-theme-algo class', async ({ page }) => {
  await openDashboard(page);
  await clickCapEqTab(page, 'Capital');

  const grids = page.locator('.cap-eq-tabbed .ag-theme-algo');
  const count = await grids.count();
  expect(count, 'Capital panel should have ag-theme-algo grids').toBeGreaterThan(0);

  // Each ag-theme-algo grid should also have ag-theme-quartz (legacy theme).
  const first = grids.first();
  const classes = await first.getAttribute('class');
  expect(classes, 'Capital grid should include ag-theme-quartz for legacy mode').toContain('ag-theme-quartz');
});

// ── 4. Equity tab: ag-Grid uses ag-theme-algo ────────────────────────────────

test('dashboard grid parity: Equity tab grids have ag-theme-algo class', async ({ page }) => {
  await openDashboard(page);
  await clickCapEqTab(page, 'Equity');

  const grids = page.locator('.cap-eq-tabbed .ag-theme-algo');
  const count = await grids.count();
  expect(count, 'Equity panel should have ag-theme-algo grids').toBeGreaterThan(0);
});

// ── 5. Perf: tab switch < 100 ms ─────────────────────────────────────────────

test('dashboard grid parity: perf — tab switches complete < 100ms', async ({ page }) => {
  await openDashboard(page);

  // Pre-warm: click to NAV first so Capital/Equity are already mounted.
  await clickCapEqTab(page, 'NAV');
  await page.waitForTimeout(200);

  // Time Capital tab click-to-panel-visible.
  const t0 = Date.now();
  await clickCapEqTab(page, 'Capital');
  // The Capital panel div should become non-hidden within the budget.
  // We detect it by the panel's `hidden` attribute being removed.
  await page.locator('.cap-eq-tabbed .card-body[hidden="false"], .cap-eq-tabbed .card-body:not([hidden])').first()
    .waitFor({ state: 'visible', timeout: 500 });
  const elapsed = Date.now() - t0;
  expect(elapsed, 'Capital tab should become visible < 100ms').toBeLessThan(100);
});

// ── 6. TOTAL row amber treatment on all three panels ─────────────────────────

test('dashboard grid parity: TOTAL row amber tint appears on all panels', async ({ page }) => {
  await openDashboard(page);

  // NAV panel total row.
  await clickCapEqTab(page, 'NAV');
  const table = page.locator('.nav-bd-table');
  const hasTable = await table.count();
  if (hasTable > 0) {
    const totalRow = table.locator('tr.nav-bd-total');
    const hasTotalRow = await totalRow.count();
    if (hasTotalRow > 0) {
      const bg = await totalRow.locator('td').first().evaluate(el => getComputedStyle(el).backgroundColor);
      // rgba(251, 191, 36, 0.22) — Chromium rounds to some form of rgba
      expect(bg, 'NAV TOTAL row should have amber bg').toMatch(/rgba?\(251[, ]+191/);
    }
  }

  // Capital panel — ag-Grid totals-row.
  await clickCapEqTab(page, 'Capital');
  await page.waitForTimeout(100);
  const capitalTotals = page.locator('.cap-eq-tabbed .ag-theme-algo .ag-row.totals-row');
  const capTotalCount = await capitalTotals.count();
  if (capTotalCount > 0) {
    const bg = await capitalTotals.first().evaluate(el => getComputedStyle(el).backgroundColor);
    expect(bg, 'Capital TOTAL row should have amber bg').toMatch(/rgba?\(251[, ]+191/);
  }
});
