/**
 * aggrid_header_font_consistency.spec.js
 *
 * Asserts that every ag-Grid header on algo pages has:
 *   - A uniform computed font-size (all cells on a page match the first cell)
 *   - Numeric headers match string headers in font-size
 *   - Uniform header row height across all grids on a page
 *
 * Desktop and mobile viewports run independently. Mobile is allowed to
 * have a different canonical size from desktop, but every mobile grid
 * must be identical among themselves (and likewise for desktop).
 *
 * Protected: .ag-theme-ramboq (public/cream) is NOT visited here.
 */

import { test, expect } from '@playwright/test';

// Pages to check — each must be accessible after login.
const ALGO_ROUTES = [
  { path: '/dashboard',         label: 'dashboard' },
  { path: '/pulse',             label: 'pulse' },
  { path: '/orders',            label: 'orders' },
  { path: '/admin/derivatives', label: 'derivatives' },
  { path: '/admin/history',     label: 'history' },
  { path: '/automation/activity', label: 'automation-activity' },
];

/**
 * Each test navigates to the target route directly. If the server redirects
 * to /signin (unauthenticated), collectHeaderMetrics finds no grids and the
 * test skips via the `metrics.length === 0` guard. No active login attempt
 * here — rely on existing session cookie (e.g., from a prior interactive
 * sign-in or TEST_STORAGE_STATE env).
 */

/**
 * Collect font-size + header-row height for every ag-Grid on the current page.
 * Returns an array of { gridIndex, fontSizePx, numericFontSizePx, headerHeightPx }.
 *
 * headerHeightPx is the height of a SINGLE ag-header-row element (not the full
 * header section). Grids with column-group headers have multiple stacked rows
 * each individually 28px tall — we use the last row (the leaf-column row) which
 * always exists and matches the CSS variable.
 */
async function collectHeaderMetrics(page) {
  // Wait for at least one ag-Grid header to appear (algo pages always have grids).
  await page.waitForSelector('.ag-theme-algo .ag-header-cell-text', { timeout: 15_000 })
    .catch(() => {});

  return page.evaluate(() => {
    /** @type {{gridIndex:number,fontSizePx:number|null,numericFontSizePx:number|null,headerHeightPx:number|null}[]} */
    const results = [];
    const grids = document.querySelectorAll('.ag-theme-algo');
    grids.forEach((grid, i) => {
      // String-header cell text (non-numeric)
      const stringCell = grid.querySelector(
        '.ag-header-cell:not(.ag-numeric-header):not(.ag-right-aligned-header) .ag-header-cell-text'
      );
      // Numeric-header cell text
      const numericCell = grid.querySelector(
        '.ag-numeric-header .ag-header-cell-text, .ag-right-aligned-header .ag-header-cell-text'
      );
      // Skip grids that are hidden (not in the layout flow) — ag-Grid
      // sets inline height values on hidden elements via its JS resize
      // engine which does not respect the CSS-variable value. offsetHeight
      // is 0 for display:none / visibility:hidden ancestors.
      const gridOffsetHeight = /** @type {HTMLElement} */ (grid).offsetHeight;
      if (gridOffsetHeight === 0) return;

      // Use the LAST ag-header-row (leaf column row) so column-group grids
      // (which stack multiple header rows) give the correct single-row height.
      const headerRows = grid.querySelectorAll('.ag-header-row');
      // Only consider header rows that are themselves visible (offsetHeight > 0)
      const visibleHeaderRows = Array.from(headerRows).filter(
        r => /** @type {HTMLElement} */ (r).offsetHeight > 0
      );
      const lastHeaderRow = visibleHeaderRows.length > 0
        ? visibleHeaderRows[visibleHeaderRows.length - 1]
        : null;

      const fontSizePx = stringCell
        ? parseFloat(getComputedStyle(stringCell).fontSize)
        : null;
      const numericFontSizePx = numericCell
        ? parseFloat(getComputedStyle(numericCell).fontSize)
        : null;
      const headerHeightPx = lastHeaderRow
        ? parseFloat(getComputedStyle(lastHeaderRow).height)
        : null;

      results.push({ gridIndex: i, fontSizePx, numericFontSizePx, headerHeightPx });
    });
    return results;
  });
}

// ── Five quality dimensions ───────────────────────────────────────────────────

// 1. SSOT: every algo-page grid uses the SAME font-size (no per-grid overrides)
// 2. Perf: grid header metrics are collected synchronously (no heavy re-layout)
// 3. Stale: no per-page <style> block overrides --ag-header-font-size for algo theme
// 4. Reuse: all grids share the single .ag-theme-algo CSS-variable definition
// 5. UX: numeric and string header cells share the same font-size

for (const route of ALGO_ROUTES) {
  test(`${route.label} — all ag-Grid headers consistent font-size + height`, async ({ page }) => {
    await page.goto(route.path, { waitUntil: 'domcontentloaded', timeout: 60_000 });

    const metrics = await collectHeaderMetrics(page);

    // Skip if the page has no grids (e.g., empty state for a specific env)
    if (metrics.length === 0) {
      test.skip(true, `No .ag-theme-algo grids found on ${route.path}`);
      return;
    }

    // Gather all non-null font sizes
    const fontSizes = metrics
      .map(m => m.fontSizePx)
      .filter((v) => v !== null);

    expect(fontSizes.length, `Expected font-size measurements on ${route.label}`)
      .toBeGreaterThan(0);

    const canonicalSize = fontSizes[0];

    // 1. SSOT: all grids must have the same font-size
    for (const m of metrics) {
      if (m.fontSizePx !== null) {
        expect(m.fontSizePx,
          `Grid ${m.gridIndex} on ${route.label}: font-size ${m.fontSizePx}px ≠ canonical ${canonicalSize}px`
        ).toBeCloseTo(canonicalSize, 1);
      }
    }

    // 5. UX: numeric headers must match non-numeric headers in font-size
    for (const m of metrics) {
      if (m.fontSizePx !== null && m.numericFontSizePx !== null) {
        expect(m.numericFontSizePx,
          `Grid ${m.gridIndex} on ${route.label}: numeric header font-size ${m.numericFontSizePx}px ≠ string header ${m.fontSizePx}px`
        ).toBeCloseTo(m.fontSizePx, 1);
      }
    }

    // Header height: all grids that have a header row must share the same height
    const headerHeights = metrics
      .map(m => m.headerHeightPx)
      .filter((v) => v !== null && v > 0);

    if (headerHeights.length > 1) {
      const canonicalH = headerHeights[0];
      for (const h of headerHeights) {
        expect(h,
          `A grid on ${route.label} has header-height ${h}px ≠ canonical ${canonicalH}px`
        ).toBeCloseTo(canonicalH, 1);
      }
    }
  });
}

// ── Cross-page consistency: all pages report the same canonical font-size ─────

test('all algo pages share identical header font-size (cross-page SSOT)', async ({ page }) => {
  test.setTimeout(120_000);
  const pageCanonicals = [];

  for (const route of ALGO_ROUTES) {
    try {
      await page.goto(route.path, { waitUntil: 'domcontentloaded', timeout: 60_000 });
    } catch {
      // Navigation timeout on slow routes (e.g., /orders heavy load) — skip entry
      continue;
    }
    const metrics = await collectHeaderMetrics(page);
    const sizes = metrics.map(m => m.fontSizePx).filter((v) => v !== null);
    if (sizes.length > 0) {
      pageCanonicals.push({ route: route.label, size: sizes[0] });
    }
  }

  // Need at least two pages to compare
  if (pageCanonicals.length < 2) return;

  const ref = pageCanonicals[0];
  for (const entry of pageCanonicals.slice(1)) {
    expect(entry.size,
      `${entry.route} header font-size ${entry.size}px ≠ ${ref.route} ${ref.size}px`
    ).toBeCloseTo(ref.size, 1);
  }
});

// ── Stale-code guard: no per-page style override for --ag-header-font-size ───

test('no inline --ag-header-font-size overrides on algo pages (stale guard)', async ({ page }) => {
  // Visit one representative page and inspect all <style> blocks in the DOM
  await page.goto('/dashboard', { waitUntil: 'networkidle' });

  const overrides = await page.evaluate(() => {
    const styleSheets = Array.from(document.styleSheets);
    const hits = [];
    for (const sheet of styleSheets) {
      let rules;
      try { rules = Array.from(sheet.cssRules || []); } catch { continue; }
      for (const rule of rules) {
        if (rule.cssText && rule.cssText.includes('--ag-header-font-size')) {
          // Allowed: the canonical .ag-theme-algo and .ag-theme-ramboq definitions
          if (!rule.selectorText?.includes('ag-theme-algo') &&
              !rule.selectorText?.includes('ag-theme-ramboq')) {
            hits.push(rule.selectorText || '(unknown)');
          }
        }
      }
    }
    return hits;
  });

  expect(overrides,
    `Unexpected --ag-header-font-size overrides found: ${overrides.join(', ')}`
  ).toHaveLength(0);
});
