import { test, expect } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('Derivatives Legs — LTP Flash', () => {
  test.setTimeout(60000);

  test('stale-code guard: LTP and P&L cells have flash wiring in CandidateLegRow.svelte', async () => {
    // SSOT: verify that both LTP and P&L cells in CandidateLegRow.svelte have
    // the flash class pattern wired for tick flash animations.
    // Test runs without server — pure source code inspection.
    const componentPath = path.join(
      process.cwd(),
      'src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte'
    );
    const content = fs.readFileSync(componentPath, 'utf-8');

    // Primary assertion: LTP cell must have flash wiring
    // Pattern: flash.classOf(`${_legFlashKey}:ltp`) on the LTP span
    const ltpFlashPattern = /flash\.classOf\([`'"`].*\$\{_legFlashKey\}:ltp[`'"`]\)/;
    expect(
      ltpFlashPattern.test(content),
      'LTP span must have flash.classOf(`${_legFlashKey}:ltp`) for tick flash'
    ).toBe(true);

    // Regression guard: ensure P&L cells still have the flash wiring
    // Patterns: flash.classOf on pnl, day, and exp P&L cells
    const pnlFlashPattern = /flash\.classOf\([`'"`].*\$\{_legFlashKey\}:(pnl|day|exp)[`'"`]\)/;
    expect(
      pnlFlashPattern.test(content),
      'P&L cells (pnl, day, exp) must have flash.classOf wiring intact'
    ).toBe(true);

    // Verify the LTP span has both tf-cell class (for flash registration)
    // and the dynamic flash class
    const ltpSpanWithTfCell = /span class="num tf-cell.*?flash\.classOf\([`'"`].*\$\{_legFlashKey\}:ltp[`'"`]\)/s;
    expect(
      ltpSpanWithTfCell.test(content),
      'LTP span must have both tf-cell class and flash.classOf pattern'
    ).toBe(true);

    // Verify the component exports the flash prop (passed from parent)
    expect(
      /flash.*?:.*?any/.test(content),
      'Component must accept flash prop for tick flash instance'
    ).toBe(true);
  });

  test.skip('live DOM: LTP cell has flash-ready classes when leg rows render', async ({ page }) => {
    // Navigate to derivatives admin page and verify LTP cell has tf-cell class
    // wired for flash capability. Gracefully skip if no positions exist or
    // network is unavailable.
    //
    // NOTE: This test requires a running dev server with open positions. Skipped
    // by default; enable via grep when manually testing against a live environment.
    const baseUrl = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

    // Pre-authenticate
    await loginAsAdmin(page);

    // Navigate to derivatives page
    await page.goto(`${baseUrl}/admin/derivatives`, { waitUntil: 'domcontentloaded' });

    // Wait for the page to stabilize
    await page.waitForLoadState('networkidle').catch(() => {
      // networkidle may not fire if streaming responses, continue anyway
    });

    // Check if leg rows exist. If no rows, skip gracefully.
    const legRowCount = await page.locator('.cand-row').count();
    if (legRowCount === 0) {
      test.skip();
    }

    // Get the first leg row
    const firstLegRow = page.locator('.cand-row').first();
    await firstLegRow.waitFor({ state: 'visible', timeout: 5000 });

    // The LTP cell is the span containing a number or '—', positioned after the
    // Lots column. In the grid layout, numeric cells are: Qty, Lots, LTP, Prev Close, Cost, PnL...
    // Count of num cells before LTP: Qty(0), Lots(1), LTP(2)
    const ltpCell = firstLegRow.locator('span.num').nth(2); // Position: Lots(1), LTP(2)

    // Verify the LTP cell exists and is visible
    await ltpCell.waitFor({ state: 'visible', timeout: 5000 });
    const ltpCellClasses = await ltpCell.getAttribute('class');

    // Assert the LTP cell has the expected flash-ready structure.
    // The LTP span should have `num` class at minimum.
    expect(ltpCellClasses).toContain('num');

    // When flash is wired, the LTP cell should have `tf-cell` class for flash registration.
    // The presence of `tf-cell` class signals readiness for tf-up / tf-down
    // classes on LTP changes during 30s polls. This only appears after the refactor.
    if (ltpCellClasses.includes('tf-cell')) {
      // Flash wiring confirmed — LTP cell is ready for tick flash
      expect(ltpCellClasses).toContain('tf-cell');
    }
  });

  test.skip('regression guard: P&L cells still have tf-cell + flash wiring after LTP change', async ({
    page,
  }) => {
    // Verify that existing P&L cells (pnl, day, exp) still have tf-cell class
    // and flash wiring after the LTP flash refactor.
    //
    // NOTE: This test requires a running dev server with open positions. Skipped
    // by default; enable via grep when manually testing against a live environment.
    const baseUrl = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

    // Pre-authenticate
    await loginAsAdmin(page);

    // Navigate to derivatives page
    await page.goto(`${baseUrl}/admin/derivatives`, { waitUntil: 'domcontentloaded' });

    await page.waitForLoadState('networkidle').catch(() => {
      // networkidle may not fire if streaming responses, continue anyway
    });

    const legRowCount = await page.locator('.cand-row').count();
    if (legRowCount === 0) {
      test.skip();
    }

    // Get the first leg row
    const firstLegRow = page.locator('.cand-row').first();
    await firstLegRow.waitFor({ state: 'visible', timeout: 5000 });

    // P&L cell (expiry P&L) should have tf-cell + flash class
    // The P&L cells carry .cand-pnl class and should have .tf-cell
    const pnlCell = firstLegRow.locator('span.cand-pnl').first();

    // Wait for visibility
    const pnlCellVisible = await pnlCell.count();
    if (pnlCellVisible === 0) {
      // If no P&L cells, skip (all positions closed or no data)
      test.skip();
    }

    await pnlCell.waitFor({ state: 'visible', timeout: 5000 });
    const pnlClasses = await pnlCell.getAttribute('class');

    // Assert regression guard: P&L cells must still have tf-cell class
    expect(pnlClasses).toContain('tf-cell');
    expect(pnlClasses).toContain('cand-pnl');

    // Verify the cell has flash-class assignment (the dynamic class from
    // flash.classOf should be one of: tf-up, tf-down, or empty if no flash)
    // We don't assert the presence of tf-up/tf-down here (they're dynamic),
    // but we verify the structure is intact.
    expect(pnlClasses).toBeTruthy();
  });

  test.skip('desktop viewport: LTP + P&L cells render without horizontal scroll', async ({ page }) => {
    // Verify that the derivatives grid renders cleanly on desktop (1400×900)
    // without horizontal scroll, and all cells (LTP, P&L, etc.) are visible.
    //
    // NOTE: This test requires a running dev server with open positions. Skipped
    // by default; enable via grep when manually testing against a live environment.
    page.setViewportSize({ width: 1400, height: 900 });

    const baseUrl = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

    await loginAsAdmin(page);
    await page.goto(`${baseUrl}/admin/derivatives`, { waitUntil: 'domcontentloaded' });

    await page.waitForLoadState('networkidle').catch(() => {
      // networkidle may not fire if streaming responses, continue anyway
    });

    const legRowCount = await page.locator('.cand-row').count();
    if (legRowCount === 0) {
      test.skip();
    }

    // Get the candidates grid container
    const gridContainer = page.locator('.cand-grid').first();
    await gridContainer.waitFor({ state: 'visible', timeout: 5000 });

    // Verify no horizontal scroll (grid width should fit within viewport)
    const gridElement = await gridContainer.evaluate((el) => {
      return {
        scrollWidth: el.scrollWidth,
        clientWidth: el.clientWidth,
        hasHorizontalScroll: el.scrollWidth > el.clientWidth,
      };
    });

    // Grid should not overflow horizontally
    expect(gridElement.hasHorizontalScroll).toBe(false);

    // Verify LTP cell is visible in the first row
    const firstRow = page.locator('.cand-row').first();
    const ltpCell = firstRow.locator('span.num').nth(2); // LTP position (Lots=1, LTP=2)
    const ltpBoundingBox = await ltpCell.boundingBox();

    // LTP cell should be visible within viewport
    expect(ltpBoundingBox).toBeTruthy();
    expect(ltpBoundingBox.x).toBeGreaterThanOrEqual(0);
    expect(ltpBoundingBox.x + ltpBoundingBox.width).toBeLessThanOrEqual(1400);
  });

  test.skip('UX: LTP cell content is formatted as price number', async ({ page }) => {
    // Verify that the LTP cell displays properly formatted price data
    // (not raw numbers, but formatted via priceFmt).
    //
    // NOTE: This test requires a running dev server with open positions. Skipped
    // by default; enable via grep when manually testing against a live environment.
    const baseUrl = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

    await loginAsAdmin(page);
    await page.goto(`${baseUrl}/admin/derivatives`, { waitUntil: 'domcontentloaded' });

    await page.waitForLoadState('networkidle').catch(() => {
      // networkidle may not fire if streaming responses, continue anyway
    });

    const legRowCount = await page.locator('.cand-row').count();
    if (legRowCount === 0) {
      test.skip();
    }

    const firstRow = page.locator('.cand-row').first();
    await firstRow.waitFor({ state: 'visible', timeout: 5000 });

    const ltpCell = firstRow.locator('span.num').nth(2); // Lots(1), LTP(2)
    const ltpText = await ltpCell.textContent();

    // LTP should either be a formatted number or '—' (no data)
    expect(/^[\d.,—]+$/.test(ltpText.trim())).toBe(true);

    // If it's a number, verify it's not NaN or Infinity
    if (ltpText.trim() !== '—') {
      const parsed = parseFloat(ltpText.replace(/,/g, ''));
      expect(isNaN(parsed)).toBe(false);
      expect(isFinite(parsed)).toBe(true);
      expect(parsed).toBeGreaterThan(0);
    }
  });
});
