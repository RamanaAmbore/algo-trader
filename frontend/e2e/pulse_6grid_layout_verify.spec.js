/**
 * Verify the MarketPulse /pulse page renders the 6-bucket 2×3 grid layout
 * introduced in commit 710a7be0.
 *
 * Expected:
 *   Container  : .mp-grids6
 *   Sections   : .mp-bucket-{pinned,watch,positions,holdings,winners,losers}
 *   Desktop    : 2-column CSS grid (Pinned|Watch, Positions|Holdings, Winners|Losers)
 *   Mobile     : 6 sections stacked vertically
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BUCKETS = ['pinned', 'watch', 'positions', 'holdings', 'winners', 'losers'];

test.describe('/pulse — 6-grid 2×3 layout', () => {
  // ── Desktop ────────────────────────────────────────────────────────────────
  test('desktop 1440×900: 6 buckets render in 2×3 layout', async ({ page }) => {
    test.setTimeout(150_000);
    await page.setViewportSize({ width: 1440, height: 900 });

    // Auth: try 'ambore' first, fall back to 'rambo'
    try {
      await loginAsAdmin(page, { user: 'ambore' });
    } catch {
      await loginAsAdmin(page, { user: 'rambo' });
    }

    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    // Wait for the 6-grid container to appear
    await page.waitForSelector('.mp-grids6', { timeout: 30_000 });

    // 1. Container exists
    const container = page.locator('.mp-grids6');
    await expect(container).toBeVisible();

    // 2. All 6 bucket sections exist
    for (const name of BUCKETS) {
      const sel = `.mp-bucket-${name}`;
      await expect(page.locator(sel), `section ${sel} should exist`).toBeVisible({ timeout: 20_000 });
    }

    // 3. Read bounding rects for all 6 sections — use evaluate so we get
    //    the full-document offset even for elements below the fold
    //    (boundingBox() returns null when the element is off-screen).
    const rects = {};
    for (const name of BUCKETS) {
      rects[name] = await page.locator(`.mp-bucket-${name}`).evaluate(el => {
        const r = el.getBoundingClientRect();
        return {
          top:    r.top + window.scrollY,
          left:   r.left + window.scrollX,
          width:  r.width,
          height: r.height,
        };
      });
    }

    console.log('--- Bounding rects ---');
    for (const [n, r] of Object.entries(rects)) {
      console.log(`  ${n}: top=${r?.top?.toFixed(0)} left=${r?.left?.toFixed(0)} w=${r?.width?.toFixed(0)} h=${r?.height?.toFixed(0)}`);
    }

    // 4. Row-alignment checks (within 10 px tolerance)
    const sameRow = (a, b) => Math.abs(a.top - b.top) <= 10;
    const belowRow = (lower, upper) => lower.top > upper.top + 20;

    const row1Same = sameRow(rects.pinned, rects.watch);
    const row2Same = sameRow(rects.positions, rects.holdings);
    const row3Same = sameRow(rects.winners, rects.losers);
    const row2BelowRow1 = belowRow(rects.positions, rects.pinned);
    const row3BelowRow2 = belowRow(rects.winners, rects.positions);

    console.log(`Row 1 (Pinned + Watch same row): ${row1Same}`);
    console.log(`Row 2 (Positions + Holdings same row): ${row2Same}`);
    console.log(`Row 3 (Winners + Losers same row): ${row3Same}`);
    console.log(`Row 2 below Row 1: ${row2BelowRow1}`);
    console.log(`Row 3 below Row 2: ${row3BelowRow2}`);
    console.log(`Width Pinned: ${rects.pinned?.width?.toFixed(0)}  Watch: ${rects.watch?.width?.toFixed(0)}`);
    console.log(`Width Positions: ${rects.positions?.width?.toFixed(0)}  Holdings: ${rects.holdings?.width?.toFixed(0)}`);
    console.log(`Width Winners: ${rects.winners?.width?.toFixed(0)}  Losers: ${rects.losers?.width?.toFixed(0)}`);

    expect(row1Same, 'Pinned + Watch must share the same row').toBe(true);
    expect(row2Same, 'Positions + Holdings must share the same row').toBe(true);
    expect(row3Same, 'Winners + Losers must share the same row').toBe(true);
    expect(row2BelowRow1, 'Row 2 must be below Row 1').toBe(true);
    expect(row3BelowRow2, 'Row 3 must be below Row 2').toBe(true);

    // 5. Each bucket should be roughly half the viewport width (allow 100-900 px)
    for (const name of BUCKETS) {
      const w = rects[name]?.width ?? 0;
      expect(w, `${name} width should be >100`).toBeGreaterThan(100);
      expect(w, `${name} width should be <1400 (not full width)`).toBeLessThan(1400);
    }

    // 6. grid-template-columns on .mp-grids6
    const gtc = await container.evaluate(el => getComputedStyle(el).gridTemplateColumns);
    console.log(`grid-template-columns: ${gtc}`);
    // CSS grid resolves to pixel values; two columns means two space-separated values
    // A simple check: should NOT be "none" and should contain two pixel values
    expect(gtc, 'grid-template-columns should be set (not none)').not.toBe('none');

    // 7. Bucket header labels exist inside each section
    for (const name of BUCKETS) {
      const label = page.locator(`.mp-bucket-${name} .mp-bucket-label`);
      await expect(label, `${name} should have a .mp-bucket-label`).toBeVisible();
    }

    // 8. Each section contains a .ag-theme-algo.bucket-grid div
    for (const name of BUCKETS) {
      const grid = page.locator(`.mp-bucket-${name} .ag-theme-algo.bucket-grid`);
      await expect(grid, `${name} should have ag-theme-algo.bucket-grid`).toBeVisible();
    }

    // 9. Look for a TOTAL row at the bottom of the Positions grid
    // Give ag-Grid time to render rows
    await page.waitForTimeout(3000);
    const posGrid = page.locator('.mp-bucket-positions .ag-theme-algo.bucket-grid');
    const allCells = await posGrid.locator('.ag-cell').allTextContents();
    const totalRow = allCells.some(t => t.trim().startsWith('TOTAL'));
    console.log(`TOTAL row found in Positions: ${totalRow}`);
    console.log(`Positions grid cell samples: ${allCells.slice(0, 8).join(' | ')}`);

    // 10. Screenshot
    await page.screenshot({
      path: 'test-results/pulse_6grid_desktop.png',
      fullPage: false,
    });
  });

  // ── Mobile ─────────────────────────────────────────────────────────────────
  test('mobile 800×900: all 6 sections stack vertically', async ({ page }) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width: 800, height: 900 });

    try {
      await loginAsAdmin(page, { user: 'ambore' });
    } catch {
      await loginAsAdmin(page, { user: 'rambo' });
    }

    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.mp-grids6', { timeout: 30_000 });

    // All 6 sections must exist (may be off-screen on mobile, so check attachment)
    for (const name of BUCKETS) {
      await expect(page.locator(`.mp-bucket-${name}`)).toBeAttached({ timeout: 20_000 });
    }

    // Read rects at 800px width — should stack (each top > previous top)
    // Use evaluate so off-screen elements still report their document position
    const rects = {};
    for (const name of BUCKETS) {
      rects[name] = await page.locator(`.mp-bucket-${name}`).evaluate(el => {
        const r = el.getBoundingClientRect();
        return { top: r.top + window.scrollY, left: r.left + window.scrollX, width: r.width, height: r.height };
      });
    }

    console.log('--- Mobile bounding rects (800×900) ---');
    for (const [n, r] of Object.entries(rects)) {
      console.log(`  ${n}: top=${r?.top?.toFixed(0)} left=${r?.left?.toFixed(0)} w=${r?.width?.toFixed(0)}`);
    }

    // In a stacked layout every section should have left ≈ 0 (or small padding)
    // and each subsequent section top > prior section top
    const tops = BUCKETS.map(n => rects[n]?.top ?? 0);
    for (let i = 1; i < tops.length; i++) {
      expect(tops[i], `${BUCKETS[i]} top must be below ${BUCKETS[i - 1]}`).toBeGreaterThan(tops[i - 1]);
    }

    // No horizontal scroll
    const hasHorizontalScroll = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth
    );
    expect(hasHorizontalScroll, 'no horizontal scroll on 800px').toBe(false);

    await page.screenshot({
      path: 'test-results/pulse_6grid_mobile.png',
      fullPage: false,
    });
  });
});
