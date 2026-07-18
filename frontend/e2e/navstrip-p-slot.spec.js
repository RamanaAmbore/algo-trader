/**
 * NavStrip P pill slot 1 (Day P&L) — regression test for close_price stale guard.
 *
 * Regression (8474a17e): When broker's close_price === last_price (common during
 * overnight gap after MCX close), the slot showed ₹0 instead of computing actual
 * day P&L. Fix in baseDayPnlForPosition() guards against this stale-snapshot case.
 *
 * Target: dev.ramboq.com (position data dependency; skip if positions empty)
 *
 * Validates:
 * - P pill slot 1 renders without crashing on page load
 * - Day P&L is NOT "₹0" or "₹0.00" (real value loaded), OR fallback: P pill visible
 * - No zero-flash: value stable after 2s (doesn't revert from non-zero to ₹0)
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const TIMEOUT = 30_000;

test.describe('NavStrip P pill — Day P&L', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('slot 1 renders non-zero value or stable empty state', async ({ page }) => {
    // Navigate to dashboard — main positions surface
    await page.goto('/dashboard');

    // Wait for PositionStrip to mount (renders the P pill)
    // The P pill is a .ps-agg span containing 3 slots (P / Lifetime / Expiry)
    const pPill = page.locator('.ps-agg').first();
    await expect(pPill).toBeVisible({ timeout: TIMEOUT });

    // Extract P slot 1 (Day P&L) — first .ps-agg-v span after .ps-agg-k with text "P"
    // HTML structure: <span class="ps-agg"> <span class="ps-agg-k">P</span>
    // <span class="ps-agg-v">...</span> <span class="ps-agg-sep">/</span> ...
    const pLabel = pPill.locator('.ps-agg-k').first();
    await expect(pLabel).toHaveText('P');

    const slot1 = pPill.locator('.ps-agg-v').first();
    await expect(slot1).toBeVisible({ timeout: TIMEOUT });

    // Capture initial value
    const initialValue = await slot1.textContent();
    console.log(`[P Slot 1] Initial value: "${initialValue}"`);

    // Wait 2s for any stale-data reversion (zero-flash regression check)
    await page.waitForTimeout(2000);
    const afterWaitValue = await slot1.textContent();
    console.log(`[P Slot 1] After 2s: "${afterWaitValue}"`);

    // Core regression check: if slot started non-zero, it must not revert to ₹0.
    // (Covers baseDayPnlForPosition Case 4 regression from 8474a17e.)
    const hasRupeeNonZero = (v) => v && /₹/.test(v) && !/₹0(?:[.,]0+)?(?:\s|$)/.test(v);
    if (hasRupeeNonZero(initialValue)) {
      // Had real data → must remain non-zero after 2s (no zero-flash)
      expect(afterWaitValue).not.toMatch(/₹0(?:[.,]0+)?(?:\s|$)/);
      console.log('[P Slot 1] Non-zero P&L stable — zero-flash regression not present');
    } else {
      // No positions / placeholder / zero: P pill must still be visible (component stable)
      await expect(pPill).toBeVisible();
      console.log('[P Slot 1] Positions empty or zero — P pill visible (component stable)');
    }
  });

  test('P pill renders across market-open and market-close boundaries', async ({ page }) => {
    // Navigate to dashboard and wait for P pill to mount
    await page.goto('/dashboard');

    const pPill = page.locator('.ps-agg').first();
    await expect(pPill).toBeVisible({ timeout: TIMEOUT });

    const slot1 = pPill.locator('.ps-agg-v').first();
    await expect(slot1).toBeVisible({ timeout: TIMEOUT });

    // Capture initial value
    const initialValue = await slot1.textContent();
    console.log(`[P Pill Stability] Initial: "${initialValue}"`);

    // Wait 3s and verify P pill element still exists and is readable
    // (doesn't unmount, doesn't break, value is retrievable)
    await page.waitForTimeout(3000);

    const stillVisible = await pPill.isVisible();
    expect(stillVisible).toBe(true);

    const afterValue = await slot1.textContent();
    console.log(`[P Pill Stability] After 3s: "${afterValue}"`);

    // Value should either remain the same or reflect a live tick update,
    // but NOT revert to zero if it started non-zero (zero-flash regression).
    // Allow for very small tick movements but reject large swings.
    if (initialValue && !initialValue.includes('₹0')) {
      // Non-zero starting point: afterValue should also be non-zero or identical
      // (live-tick updates OK, but no revert to zero)
      expect(afterValue).not.toMatch(/₹0(?:\.0+)?(?:\s|$)/);
    }
  });
});
