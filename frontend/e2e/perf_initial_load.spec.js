// @ts-check
//
// Diagnostic e2e: does PerformancePage actually populate ag-Grid rows
// on initial load (no manual Refresh click) ?
//
// Tests the public /performance route AND the algo /dashboard route on
// whichever PLAYWRIGHT_BASE_URL is set (https://ramboq.com for prod).
// Runs unauthenticated — on prod, /dashboard renders demo mode with
// real-but-masked broker data; both routes should populate the grids
// from the API on mount.
//
// Run:
//   PLAYWRIGHT_BASE_URL=https://ramboq.com npx playwright test \
//     e2e/perf_initial_load.spec.js --project=chromium-desktop
//
// Reports per-grid row counts, captures every console.log + console.error
// line, and emits a screenshot whenever a grid is empty after the wait
// window. The screenshot lands in test-results/ and the console output
// is interleaved with the grid assertions in stdout.

import { test, expect } from '@playwright/test';

/** @param {import('@playwright/test').Page} page */
function attachConsoleLogger(page) {
  /** @type {string[]} */
  const log = [];
  page.on('console', (msg) => {
    const line = `[${msg.type()}] ${msg.text()}`;
    log.push(line);
  });
  page.on('pageerror', (err) => log.push(`[pageerror] ${err.message}`));
  return log;
}

/** Wait until at least one of the given selectors has any rows, OR
 *  the timeout is hit. Returns the per-selector row count map. */
async function probeGrids(page, selectors, timeout = 15_000) {
  const start = Date.now();
  /** @type {Record<string, number>} */
  let counts = {};
  while (Date.now() - start < timeout) {
    counts = {};
    for (const [label, sel] of Object.entries(selectors)) {
      counts[label] = await page.locator(`${sel} .ag-row`).count();
    }
    if (Object.values(counts).some((c) => c > 0)) return counts;
    await page.waitForTimeout(500);
  }
  return counts;
}

test.describe('Performance page — initial load populates grids', () => {

  test('public /performance: ag-Grid rows arrive without clicking Refresh', async ({ page }, testInfo) => {
    const consoleLog = attachConsoleLogger(page);

    await page.goto('/performance', { waitUntil: 'domcontentloaded' });

    // Each grid is rendered into a div with `class="ag-theme-quartz <theme>"`.
    // We don't have stable test ids, so probe by .ag-theme-* selectors and
    // count visible .ag-row children.
    const counts = await probeGrids(page, {
      anyGrid: '.ag-theme-quartz',
    }, 20_000);

    console.log('====[ /performance ]====');
    console.log('Row counts:', counts);
    console.log('--- browser console (last 40 lines) ---');
    for (const line of consoleLog.slice(-40)) console.log(line);

    if ((counts.anyGrid ?? 0) === 0) {
      // Empty grids — capture state for diagnosis.
      const path = testInfo.outputPath('performance-empty.png');
      await page.screenshot({ path, fullPage: true });
      console.log('Screenshot saved:', path);
    }

    // Assert SOME grid populated. Demo mode on prod always has real
    // broker data, so a 0-row outcome is the bug we're hunting.
    expect(counts.anyGrid, 'Expected at least one populated AG Grid row on /performance').toBeGreaterThan(0);
  });

  test('algo /dashboard: ag-Grid rows arrive without clicking Refresh', async ({ page }, testInfo) => {
    const consoleLog = attachConsoleLogger(page);

    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    const counts = await probeGrids(page, {
      anyGrid: '.ag-theme-quartz',
    }, 20_000);

    console.log('====[ /dashboard ]====');
    console.log('Row counts:', counts);
    console.log('--- browser console (last 40 lines) ---');
    for (const line of consoleLog.slice(-40)) console.log(line);

    if ((counts.anyGrid ?? 0) === 0) {
      const path = testInfo.outputPath('dashboard-empty.png');
      await page.screenshot({ path, fullPage: true });
      console.log('Screenshot saved:', path);
    }

    expect(counts.anyGrid, 'Expected at least one populated AG Grid row on /dashboard').toBeGreaterThan(0);
  });
});
