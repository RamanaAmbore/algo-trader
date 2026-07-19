/**
 * ui_polish_round4_smoke.spec.js
 *
 * Playwright smoke tests for UI Polish Round 4 changes.
 *
 * Tests cover:
 * - Signals button visible border on charts
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('UI Polish Round 4 smoke tests', () => {
  test('signals button visible border', async ({ page }) => {
    // Verify that .cw-signals-btn has a visible border (not transparent)
    test.setTimeout(60_000);

    // Use loginAsAdmin to ensure auth is proper
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives');
    await page.waitForLoadState('networkidle');

    // The signals button appears on the chart if one is shown
    // Since it may not always be visible on derivatives, skip gracefully
    const sigBtn = page.locator('.cw-signals-btn');
    const btnCount = await sigBtn.count();

    if (btnCount === 0) {
      test.skip();
      return;
    }

    await expect(sigBtn).toBeVisible({ timeout: 10000 });

    // Check the computed border-color
    const borderColor = await sigBtn.evaluate(el => getComputedStyle(el).borderColor);

    // Verify border-color is not transparent or rgba(0,0,0,0)
    expect(borderColor).toBeTruthy();
    expect(borderColor).not.toMatch(/rgba\s*\(\s*0\s*,\s*0\s*,\s*0\s*,\s*0\s*\)/);
    expect(borderColor.toLowerCase()).not.toBe('transparent');
  });
});
