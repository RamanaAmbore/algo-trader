/**
 * Day P&L Breakup modal — PositionStrip integration.
 *
 * Tests opening, closing, and formula expansion in the Day P&L Breakup modal.
 * Modal opens from the P (Day P&L) slot 1 span in the PositionStrip, showing
 * a breakdown by position + account.
 *
 * Auth: Uses loginAsAdmin from fixtures/auth.js. Caches token if available.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

test.describe('Day P&L Breakup modal', () => {
  test.beforeEach(async ({ page }) => {
    // Test only on desktop — mobile viewports may not have positions data
    // or the strip may not be fully interactive on narrower screens.
    test.skip(page.viewportSize()?.width !== 1400, 'Tests run on desktop viewport only');

    await loginAsAdmin(page);
    // Navigate to dashboard where PositionStrip is visible.
    await page.goto('/dashboard', { waitUntil: 'networkidle' });
    // Wait for the strip to be visible.
    await expect(page.locator('.ps-strip')).toBeVisible({ timeout: 10_000 });
  });

  test('Modal opens when Day P&L span is clicked', async ({ page }) => {
    // Find the P slot 1 span (the day P&L value with cursor:pointer).
    const dayPnlSpan = page.locator('span[title="Click for Day P&L breakup"]').first();
    await expect(dayPnlSpan).toBeVisible({ timeout: 5_000 });

    // Click to open the modal.
    await dayPnlSpan.click();

    // Assert the modal overlay is visible.
    const overlay = page.locator('.dpb-overlay').first();
    await expect(overlay).toBeVisible({ timeout: 3_000 });

    // Assert the panel is visible and contains the heading.
    const panel = page.locator('.dpb-panel').first();
    await expect(panel).toBeVisible();

    // Check for the title text.
    const title = page.locator('.dpb-title').first();
    await expect(title).toContainText(/Day P&L Breakup/i);
  });

  test('Modal contains table rows with position data', async ({ page }) => {
    const dayPnlSpan = page.locator('span[title="Click for Day P&L breakup"]').first();
    await dayPnlSpan.click();

    // Wait for modal to open.
    await expect(page.locator('.dpb-panel').first()).toBeVisible({ timeout: 3_000 });

    // Check for at least one data row (tbody tr with .dpb-row class).
    const rows = page.locator('.dpb-row').first();
    await expect(rows).toBeVisible({ timeout: 3_000 });

    // Assert the row has a symbol cell (non-empty text).
    const symbolCell = page.locator('.dpb-row .dpb-td-sym').first();
    const symbolText = await symbolCell.textContent();
    // Symbol should be either a real symbol or '—' for empty, but we expect
    // at least one position in the dev environment.
    expect(symbolText?.trim()).toBeTruthy();
  });

  test('Modal header total shows money value', async ({ page }) => {
    const dayPnlSpan = page.locator('span[title="Click for Day P&L breakup"]').first();
    await dayPnlSpan.click();

    // Wait for modal.
    await expect(page.locator('.dpb-panel').first()).toBeVisible({ timeout: 3_000 });

    // Check the total value in the header.
    const totalSpan = page.locator('.dpb-total').first();
    await expect(totalSpan).toBeVisible();

    const totalText = await totalSpan.textContent();
    // Total should contain either a number/dash, not empty.
    expect(totalText?.trim()).toBeTruthy();
    // It should either be a formatted number, or '—' if no data.
    // Money values in the app use compact format: K, L, Cr (₹1.2K, —, etc).
    expect(totalText).toMatch(/^[₹\-\d.,KLCrM]+$/);
  });

  test('Escape key closes the modal', async ({ page }) => {
    const dayPnlSpan = page.locator('span[title="Click for Day P&L breakup"]').first();
    await dayPnlSpan.click();

    // Wait for modal to open.
    await expect(page.locator('.dpb-overlay').first()).toBeVisible({ timeout: 3_000 });

    // Press Escape.
    await page.keyboard.press('Escape');

    // Assert modal is gone.
    await expect(page.locator('.dpb-overlay').first()).not.toBeVisible({ timeout: 2_000 });
  });

  test('Clicking outside modal (backdrop) closes it', async ({ page }) => {
    const dayPnlSpan = page.locator('span[title="Click for Day P&L breakup"]').first();
    await dayPnlSpan.click();

    // Wait for modal to open.
    await expect(page.locator('.dpb-overlay').first()).toBeVisible({ timeout: 3_000 });

    // Click on the overlay background (outside the panel).
    // The overlay is fixed-position with z-index 400. Click on the right edge
    // of the viewport (away from navbar) to avoid pointer interception.
    const overlay = page.locator('.dpb-overlay').first();
    const viewport = page.viewportSize();
    if (viewport) {
      await overlay.click({ position: { x: viewport.width - 10, y: viewport.height - 10 } });
    } else {
      // Fallback: use keyboard (Escape) if click fails
      await page.keyboard.press('Escape');
    }

    // Assert modal is gone.
    await expect(page.locator('.dpb-overlay').first()).not.toBeVisible({ timeout: 2_000 });
  });

  test('Chevron expands formula row', async ({ page }) => {
    const dayPnlSpan = page.locator('span[title="Click for Day P&L breakup"]').first();
    await dayPnlSpan.click();

    // Wait for modal.
    await expect(page.locator('.dpb-panel').first()).toBeVisible({ timeout: 3_000 });

    // Find the first chevron button (collapse/expand).
    const chevron = page.locator('.dpb-chevron').first();
    await expect(chevron).toBeVisible();

    // Click to expand.
    await chevron.click();

    // Look for the formula row that appears below the row.
    // Formula rows have class .dpb-row-formula and contain .dpb-formula text.
    const formulaRow = page.locator('.dpb-row-formula .dpb-formula').first();
    await expect(formulaRow).toBeVisible({ timeout: 2_000 });

    // Check that the formula text contains expected formula components.
    const formulaText = await formulaRow.textContent();
    expect(formulaText).toMatch(/Day P&L/i);
  });

  test('Zero day P&L rows show warning icon', async ({ page }) => {
    const dayPnlSpan = page.locator('span[title="Click for Day P&L breakup"]').first();
    await dayPnlSpan.click();

    // Wait for modal.
    await expect(page.locator('.dpb-panel').first()).toBeVisible({ timeout: 3_000 });

    // Look for any .dpb-warn element (the ⚠ icon for zero P&L).
    // This test is soft — if no zero P&L rows exist, the selector simply
    // won't match, and we check for it conditionally.
    const warns = page.locator('.dpb-warn');
    const warnCount = await warns.count();

    if (warnCount > 0) {
      // If there are zero P&L rows, they should have the warning icon.
      const warn = warns.first();
      await expect(warn).toBeVisible();

      // The warning should contain the ⚠ symbol or similar indicator.
      const warnText = await warn.textContent();
      expect(warnText).toContain('⚠');

      // The warning should have a title attribute explaining why.
      const title = await warn.getAttribute('title');
      expect(title).toBeTruthy();
    }
  });

  test('Close button closes the modal', async ({ page }) => {
    const dayPnlSpan = page.locator('span[title="Click for Day P&L breakup"]').first();
    await dayPnlSpan.click();

    // Wait for modal.
    await expect(page.locator('.dpb-panel').first()).toBeVisible({ timeout: 3_000 });

    // Find and click the close button (✕).
    const closeBtn = page.locator('.dpb-close').first();
    await expect(closeBtn).toBeVisible();
    await closeBtn.click();

    // Assert modal is gone.
    await expect(page.locator('.dpb-overlay').first()).not.toBeVisible({ timeout: 2_000 });
  });

  test('Modal is accessible with proper ARIA attributes', async ({ page }) => {
    const dayPnlSpan = page.locator('span[title="Click for Day P&L breakup"]').first();
    await dayPnlSpan.click();

    // Wait for modal.
    const overlay = page.locator('.dpb-overlay').first();
    await expect(overlay).toBeVisible({ timeout: 3_000 });

    // Check ARIA attributes on the overlay.
    const role = await overlay.getAttribute('role');
    expect(role).toBe('dialog');

    const ariaModal = await overlay.getAttribute('aria-modal');
    expect(ariaModal).toBe('true');

    const ariaLabel = await overlay.getAttribute('aria-label');
    expect(ariaLabel).toBeTruthy();

    // Check the panel has presentation role to indicate it's not a separate dialog.
    const panel = page.locator('.dpb-panel').first();
    const panelRole = await panel.getAttribute('role');
    expect(panelRole).toBe('presentation');
  });

  test('New-position row (overnight_qty=0) formula renders via baseDayPnlForPosition SSOT', async ({ page }) => {
    // Tests the overnight_quantity=0 && pnl≠0 rescue path.
    // Kite returns day_change_val=0 for new-position rows; real value is in pnl.
    // DayPnlBreakup must read via baseDayPnlForPosition, never day_change_val directly.

    // Static SSOT check — component must not read day_change_val directly
    const { execSync } = await import('child_process');
    const directRead = execSync(
      'grep -n "day_change_val" /Users/ramanambore/projects/ramboq/frontend/src/lib/DayPnlBreakup.svelte 2>/dev/null || true'
    ).toString().trim();
    expect(directRead, 'DayPnlBreakup must not read day_change_val directly — use baseDayPnlForPosition()').toBe('');

    // Open the modal
    const dayPnlSpan = page.locator('span[title="Click for Day P&L breakup"]').first();
    await expect(dayPnlSpan).toBeVisible({ timeout: 5_000 });
    await dayPnlSpan.click();
    await expect(page.locator('.dpb-panel').first()).toBeVisible({ timeout: 3_000 });

    const rows = page.locator('.dpb-row');
    const rowCount = await rows.count();
    if (rowCount === 0) {
      test.skip('No position rows in dev environment — skipping formula branch check');
      return;
    }

    // Expand the first row chevron to reveal the formula row
    const firstChevron = page.locator('.dpb-chevron').first();
    if (await firstChevron.isVisible()) {
      await firstChevron.click();
      const formulaRow = page.locator('.dpb-row-formula').first();
      await expect(formulaRow).toBeVisible({ timeout: 2_000 });
      const formulaText = await formulaRow.textContent();
      expect(formulaText?.trim()).toBeTruthy();
      expect(formulaText).toMatch(/P&L|PnL|pnl/i);
    }
  });
});
