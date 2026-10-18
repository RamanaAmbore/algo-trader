/**
 * aggrid_header_row_height_parity.spec.js
 *
 * 2026-10-18 item 4 (corrected) — ag-Grid header and row height are driven
 * by ONE shared CSS custom property, `--grid-row-height` (app.css), not two
 * independently-set JS gridOptions. The original fix only set the JS
 * `headerHeight` option equal to `rowHeight`; that masked the real bug,
 * which lives one layer down: ag-Grid's legacy theme actually renders off
 * its own `--ag-row-height` / `--ag-header-height` CSS vars, and those were
 * wired from TWO separate tokens (`--grid-row-height` / a since-retired
 * `--grid-header-height`) that could drift apart by accident. This spec
 * asserts the COMPUTED CSS var values are equal — the layer where the bug
 * actually lived — on both a MarketPulse (dark, `.ag-theme-algo`) grid and
 * a PerformancePage (light, `.ag-theme-ramboq`) grid, at both the desktop
 * and mobile (≤720px) breakpoints, since PerformancePage's `makeGrid()`
 * only started setting `--ag-header-height` inline (alongside the
 * pre-existing `--ag-row-height` inline set) in this corrected pass — a
 * desktop-only check can't distinguish the fix from the bug, because the
 * static `.ag-theme-ramboq` CSS literals already match at desktop.
 *
 * Still also measures the rendered `.ag-header-row` / `.ag-row` DOM heights
 * as a secondary, user-visible-facing check.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TOLERANCE_PX = 1;

/** Measure the first visible grid's leaf header-row height vs. its first data-row height. */
async function measureHeaderVsRow(page, gridSelector) {
  return page.evaluate((sel) => {
    const grids = Array.from(document.querySelectorAll(sel));
    for (const grid of grids) {
      if (/** @type {HTMLElement} */ (grid).offsetHeight === 0) continue;
      const headerRows = Array.from(grid.querySelectorAll('.ag-header-row'))
        .filter((r) => /** @type {HTMLElement} */ (r).offsetHeight > 0);
      const dataRows = Array.from(grid.querySelectorAll('.ag-row'))
        .filter((r) => /** @type {HTMLElement} */ (r).offsetHeight > 0);
      if (headerRows.length === 0 || dataRows.length === 0) continue;
      const headerRow = headerRows[headerRows.length - 1];
      const dataRow = dataRows[0];
      return {
        headerHeight: headerRow.getBoundingClientRect().height,
        rowHeight: dataRow.getBoundingClientRect().height,
      };
    }
    return null;
  }, gridSelector);
}

/**
 * Read the effective (computed, cascade-resolved) --ag-row-height and
 * --ag-header-height custom properties on the first visible grid's own
 * root wrapper. Custom properties inherit down the DOM, and the inline
 * override (JS `el.style.setProperty(...)`) lands on the grid container
 * itself, so reading from `.ag-root-wrapper` (a descendant of the
 * container ag-Grid creates) picks up whichever layer actually won —
 * inline override or the class-level `var(--grid-row-height)` mapping.
 */
async function readAgHeightVars(page, gridSelector) {
  return page.evaluate((sel) => {
    const grids = Array.from(document.querySelectorAll(sel));
    for (const grid of grids) {
      if (/** @type {HTMLElement} */ (grid).offsetHeight === 0) continue;
      const wrapper = grid.querySelector('.ag-root-wrapper') || grid;
      const cs = getComputedStyle(wrapper);
      const rowVar = cs.getPropertyValue('--ag-row-height').trim();
      const headerVar = cs.getPropertyValue('--ag-header-height').trim();
      if (!rowVar && !headerVar) continue;
      return { rowVar, headerVar };
    }
    return null;
  }, gridSelector);
}

function parsePx(v) {
  const n = parseFloat(String(v).replace('px', ''));
  return Number.isFinite(n) ? n : null;
}

test.describe('ag-Grid headerHeight === rowHeight — desktop', () => {
  test('MarketPulse (/pulse, ag-theme-algo) — header row height equals data row height', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: 60_000 });

    let m = null;
    await expect.poll(async () => {
      m = await measureHeaderVsRow(page, '.ag-theme-algo');
      return m;
    }, { timeout: 20_000, intervals: [250, 500, 1000] }).not.toBeNull();

    expect(Math.abs(m.headerHeight - m.rowHeight)).toBeLessThanOrEqual(TOLERANCE_PX);

    const vars = await readAgHeightVars(page, '.ag-theme-algo');
    expect(vars).not.toBeNull();
    expect(parsePx(vars.headerVar)).toBe(parsePx(vars.rowVar));
  });

  test('PerformancePage (/performance, public ag-theme-ramboq) — header row height equals data row height', async ({ page }) => {
    await page.goto('/performance', { waitUntil: 'domcontentloaded', timeout: 60_000 });

    let m = null;
    await expect.poll(async () => {
      m = await measureHeaderVsRow(page, '.ag-theme-ramboq');
      return m;
    }, { timeout: 20_000, intervals: [250, 500, 1000] }).not.toBeNull();

    expect(Math.abs(m.headerHeight - m.rowHeight)).toBeLessThanOrEqual(TOLERANCE_PX);

    const vars = await readAgHeightVars(page, '.ag-theme-ramboq');
    expect(vars).not.toBeNull();
    expect(parsePx(vars.headerVar)).toBe(parsePx(vars.rowVar));
  });
});

test.describe('ag-Grid headerHeight === rowHeight — mobile (≤720px)', () => {
  // _isMobile (algoGridUtils.js) and isDark/_isMobile (PerformancePage.svelte)
  // are both evaluated against window.innerWidth at module/mount time, so
  // the viewport must be set BEFORE navigation — a resize after load would
  // not be picked up by either.
  test.use({ viewport: { width: 390, height: 844 } });

  test('MarketPulse (/pulse, ag-theme-algo, mobile) — header equals row, both at the mobile value', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: 60_000 });

    let m = null;
    await expect.poll(async () => {
      m = await measureHeaderVsRow(page, '.ag-theme-algo');
      return m;
    }, { timeout: 20_000, intervals: [250, 500, 1000] }).not.toBeNull();

    expect(Math.abs(m.headerHeight - m.rowHeight)).toBeLessThanOrEqual(TOLERANCE_PX);

    const vars = await readAgHeightVars(page, '.ag-theme-algo');
    expect(vars).not.toBeNull();
    expect(parsePx(vars.headerVar)).toBe(parsePx(vars.rowVar));
    // Mobile --grid-row-height SSOT value (app.css @media max-width:720px).
    expect(parsePx(vars.rowVar)).toBe(34);
  });

  test('PerformancePage (/performance, ag-theme-ramboq, mobile) — header equals row, both at the mobile value', async ({ page }) => {
    await page.goto('/performance', { waitUntil: 'domcontentloaded', timeout: 60_000 });

    let m = null;
    await expect.poll(async () => {
      m = await measureHeaderVsRow(page, '.ag-theme-ramboq');
      return m;
    }, { timeout: 20_000, intervals: [250, 500, 1000] }).not.toBeNull();

    expect(Math.abs(m.headerHeight - m.rowHeight)).toBeLessThanOrEqual(TOLERANCE_PX);

    const vars = await readAgHeightVars(page, '.ag-theme-ramboq');
    expect(vars).not.toBeNull();
    expect(parsePx(vars.headerVar)).toBe(parsePx(vars.rowVar));
    // PerformancePage's own independently-tuned mobile row height
    // (makeGrid()'s _rowH calc: isDark ? 34 : 32 when _isMobile).
    expect(parsePx(vars.rowVar)).toBe(32);
  });
});
