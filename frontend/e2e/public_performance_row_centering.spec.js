/**
 * Public /performance page — ag-Grid row height + vertical centering
 *
 * Bug: `.ag-theme-ramboq` (app.css) declares --ag-row-height: 22px, which
 * ag-Grid's legacy-theme cell CSS uses to derive `line-height` for
 * vertical centering of cell text. PerformancePage.svelte's makeGrid()
 * hardcoded `rowHeight: 28` at the JS level, orphaning the theme's own
 * 22px var — rows rendered 28px tall with a stale 19px line-height,
 * so text sat visibly above the row's vertical midpoint instead of
 * centered, and the row was taller than the 10px (0.625rem) font needs.
 *
 * Fix: makeGrid() now computes rowHeight and writes it straight to
 * --ag-row-height via el.style.setProperty, so the JS rowHeight and the
 * CSS var that drives line-height centering can never drift apart again.
 *
 * This spec checks both desktop (22px rows) and mobile (36px rows,
 * unchanged touch-target convention) actually center cell text within
 * their row, and that the desktop row height is proportionate to the
 * theme's 10px font rather than oversized.
 */

import { test, expect } from '@playwright/test';

/** Poll the DOM for the first populated (non-empty) data row in a visible
 *  .ag-theme-ramboq grid, then measure row vs. text-node vertical geometry. */
async function measureFirstDataRow(page) {
  return page.evaluate(() => {
    const grids = Array.from(document.querySelectorAll('.ag-theme-ramboq'));
    for (const g of grids) {
      if (g.classList.contains('hidden')) continue;
      const rows = Array.from(g.querySelectorAll('.ag-row:not(.totals-row)'));
      for (const row of rows) {
        const cell = row.querySelector('.ag-cell');
        if (!cell) continue;
        const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
        const tn = walker.nextNode();
        if (!tn || !tn.textContent.trim()) continue;

        const rowRect = row.getBoundingClientRect();
        if (rowRect.height === 0) continue;

        const range = document.createRange();
        range.selectNodeContents(tn);
        const textRect = range.getBoundingClientRect();
        const cellStyle = getComputedStyle(cell);

        return {
          rowHeight: rowRect.height,
          rowMidpoint: rowRect.top + rowRect.height / 2,
          textMidpoint: textRect.top + textRect.height / 2,
          cellLineHeight: parseFloat(cellStyle.lineHeight),
          cellFontSize: parseFloat(cellStyle.fontSize),
        };
      }
    }
    return null;
  });
}

test.describe('Public /performance page — ag-Grid row height + centering', () => {
  // Row height/centering genuinely differs by viewport (_isMobile is
  // evaluated once at module load from window.innerWidth), so this
  // sub-describe pins a real desktop viewport regardless of which
  // Playwright project (including mobile-portrait/-landscape) runs it —
  // same pattern as the mobile-portrait sub-describe below.
  test.describe('desktop', () => {
    test.use({ viewport: { width: 1400, height: 900 } });

    test('row is proportionate to font size and text is vertically centered', async ({ page }) => {
      await page.goto('/performance');
      await page.locator('.ag-theme-ramboq').first().waitFor({ state: 'attached', timeout: 15_000 });

      let m = null;
      await expect.poll(async () => {
        m = await measureFirstDataRow(page);
        return m;
      }, { timeout: 20_000, intervals: [250, 500, 1000] }).not.toBeNull();

      // Row height: proportionate to the 10px (0.625rem) theme font — not
      // the previous 28px. 22px matches --ag-row-height in app.css.
      expect(m.rowHeight).toBeGreaterThanOrEqual(20);
      expect(m.rowHeight).toBeLessThanOrEqual(24);

      // Root-cause invariant: line-height must track the actual row height
      // (ag-Grid's legacy formula is ~rowHeight - 3px), not a stale
      // orphaned value. This is the specific thing that regressed.
      expect(m.cellLineHeight).toBeGreaterThanOrEqual(m.rowHeight - 5);
      expect(m.cellLineHeight).toBeLessThanOrEqual(m.rowHeight - 1);

      // Vertical centering: text midpoint within 2px of the row midpoint.
      expect(Math.abs(m.textMidpoint - m.rowMidpoint)).toBeLessThanOrEqual(2);
    });
  });

  test.describe('mobile-portrait', () => {
    test.use({ viewport: { width: 360, height: 800 } });

    test('mobile: 36px touch-target row still centers text', async ({ page }) => {
      await page.goto('/performance');
      await page.locator('.ag-theme-ramboq').first().waitFor({ state: 'attached', timeout: 15_000 });

      let m = null;
      await expect.poll(async () => {
        m = await measureFirstDataRow(page);
        return m;
      }, { timeout: 20_000, intervals: [250, 500, 1000] }).not.toBeNull();

      // Mobile row height convention is unchanged (36px touch target).
      expect(m.rowHeight).toBeGreaterThanOrEqual(34);
      expect(m.rowHeight).toBeLessThanOrEqual(38);

      expect(m.cellLineHeight).toBeGreaterThanOrEqual(m.rowHeight - 5);
      expect(m.cellLineHeight).toBeLessThanOrEqual(m.rowHeight - 1);

      expect(Math.abs(m.textMidpoint - m.rowMidpoint)).toBeLessThanOrEqual(2);
    });
  });
});
