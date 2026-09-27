/**
 * Regression guard for a live defect on /pulse: Holdings/Positions rows
 * rendered on top of each other after a row-order change (e.g. a column
 * sort click, or a poll that adds/reorders rows), with every cell in the
 * overlapping slot showing two blended values.
 *
 * Root cause: ag-Grid's `animateRows` option defaults to `true`, which
 * applies a 0.4s CSS transform transition (`ag-grid.css`'s
 * `.ag-row-animation .ag-row { transition: transform 0.4s, ... }`) to every
 * row whenever the row set/order changes. MarketPulse's bucket grids
 * (Pinned/Watch/Positions/Holdings/Winners/Losers, all built via
 * `makeBucketGrid` in MarketPulse.svelte) replace `rowData` wholesale on
 * every poll — a routine reorder, not an edge case — so this produced a
 * real, reproducible overlap: two `.ag-row` elements sitting within a few
 * px of each other mid-transition, each showing a different row's data.
 *
 * Fix: `animateRows: false` in `makeBucketGrid` (MarketPulse.svelte). This
 * test forces a reorder (column-header sort click) and samples every
 * `.ag-row`'s bounding-box `top` via requestAnimationFrame across the
 * transition window, asserting no two rows ever sit within a few px of
 * each other — a structural guard, not a screenshot diff.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const OVERLAP_THRESHOLD_PX = 3;
const SAMPLE_WINDOW_MS = 1200;

/**
 * Sample every `.ag-row`'s bounding-box top inside `bucketSelector` via
 * requestAnimationFrame for SAMPLE_WINDOW_MS, and return the max count of
 * row pairs found within OVERLAP_THRESHOLD_PX of each other in any single
 * sample (0 = no overlap ever observed).
 */
async function sampleForOverlap(page, bucketSelector) {
  return page.evaluate(
    async ({ bucketSelector, thresholdPx, windowMs }) => {
      const root = document.querySelector(`${bucketSelector} .ag-root-wrapper`);
      if (!root) return { maxOverlapCount: -1, example: null };
      let maxOverlapCount = 0;
      let example = null;
      const start = performance.now();
      while (performance.now() - start < windowMs) {
        const rows = Array.from(root.querySelectorAll('.ag-center-cols-container .ag-row'));
        const tops = rows
          .map((r) => ({ id: r.getAttribute('row-id'), top: r.getBoundingClientRect().top }))
          .sort((a, b) => a.top - b.top);
        let count = 0;
        for (let i = 1; i < tops.length; i++) {
          if (Math.abs(tops[i].top - tops[i - 1].top) < thresholdPx) {
            count++;
            if (!example) example = { a: tops[i - 1], b: tops[i] };
          }
        }
        maxOverlapCount = Math.max(maxOverlapCount, count);
        await new Promise((res) => requestAnimationFrame(res));
      }
      return { maxOverlapCount, example };
    },
    { bucketSelector, thresholdPx: OVERLAP_THRESHOLD_PX, windowMs: SAMPLE_WINDOW_MS }
  );
}

async function forceReorderAndAssertNoOverlap(page, bucketSelector, headerText) {
  const root = page.locator(`${bucketSelector} .ag-root-wrapper`);
  await expect(root).toBeVisible({ timeout: 20_000 });

  const header = page.locator(`${bucketSelector} .ag-header-cell`).filter({ hasText: headerText }).first();
  await header.click();

  const { maxOverlapCount, example } = await sampleForOverlap(page, bucketSelector);
  expect(maxOverlapCount, `overlapping row pair found: ${JSON.stringify(example)}`).toBe(0);
}

test.describe('/pulse — bucket-grid rows never overlap on reorder', () => {
  test('Holdings grid: no two rows share a vertical position after a sort click', async ({ page }) => {
    test.setTimeout(90_000);
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.mp-bucket-holdings', { timeout: 30_000 });
    await page.waitForTimeout(4000); // let the first data poll land

    await forceReorderAndAssertNoOverlap(page, '.mp-bucket-holdings', 'LTP');
  });

  test('Positions grid: no two rows share a vertical position after a sort click', async ({ page }) => {
    test.setTimeout(90_000);
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.mp-bucket-positions', { timeout: 30_000 });
    await page.waitForTimeout(4000);

    await forceReorderAndAssertNoOverlap(page, '.mp-bucket-positions', 'LTP');
  });

  test('bucket grids do not opt into ag-Grid row-transition animation', async ({ page }) => {
    // Structural guard for the `animateRows: false` fix itself — ag-Grid
    // only adds the `.ag-row-animation` class (which carries the transform
    // transition responsible for the overlap) when animateRows is truthy.
    test.setTimeout(60_000);
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.mp-bucket-holdings', { timeout: 30_000 });
    await page.waitForTimeout(4000);

    const animatedGrids = await page.evaluate(() => {
      const buckets = ['pinned', 'watch', 'positions', 'holdings', 'winners', 'losers'];
      return buckets
        .map((b) => {
          const root = document.querySelector(`.mp-bucket-${b} .ag-root-wrapper`);
          return root && root.classList.contains('ag-row-animation') ? b : null;
        })
        .filter(Boolean);
    });
    expect(animatedGrids, `buckets still opted into row animation: ${animatedGrids}`).toEqual([]);
  });
});
