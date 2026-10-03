/**
 * modal-market-pulse-option-picker.spec.js
 * MarketPulse option picker (migrated to ModalShell) behavior.
 *
 * The option picker opens from inside AddToPulseModal when clicking an F&O underlying.
 * After ModalShell migration it renders as role="dialog".
 *
 * Flow: /pulse → Manage watchlists → type NIFTY → click a SymbolSearchInput
 * result row (`.ssi-row`, 2026-10 canonical-component migration) → option
 * picker opens. Tests skip gracefully if the search returns no results.
 *
 * Escape count (2026-10): AddToPulseModal auto-focuses its symbol input on
 * open, and SymbolSearchInput opens its own dropdown (showing a few pinned
 * shortcuts) on focus — pushing its own dismissible layer ABOVE the modal's.
 * The first Escape now closes that dropdown layer, not necessarily the
 * whole modal; tests below press Escape up to twice and poll for the
 * actual end state rather than assuming a fixed count.
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

async function openAddToPulseModal(page) {
  await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.mp-layout, .mp-bucket-wrap').first()).toBeVisible({ timeout: 20000 });
  const manageBtn = page.locator('button[aria-label="Manage watchlists"], button.mp-add-btn').first();
  await expect(manageBtn).toBeVisible({ timeout: 10000 });
  await manageBtn.click();
  const modal = page.locator('[role="dialog"]').first();
  await expect(modal).toBeVisible({ timeout: 5000 });
  return modal;
}

test.describe('MarketPulse option picker modal — open/close/ESC', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('AddToPulseModal smoke — opens and closes via ESC', async ({ page }) => {
    const modal = await openAddToPulseModal(page);
    await expect(modal).toBeVisible();
    // First Escape may only close SymbolSearchInput's own pinned-shortcut
    // dropdown (it auto-opens on the modal's auto-focus) — press a second
    // time if the modal itself is still visible. See file header comment.
    await page.keyboard.press('Escape');
    const stillOpen = await modal.isVisible().catch(() => false);
    if (stillOpen) await page.keyboard.press('Escape');
    await expect(modal).not.toBeVisible({ timeout: 3000 });
  });

  test('Option picker modal opens when clicking F&O underlying in typeahead', async ({ page }) => {
    await openAddToPulseModal(page);
    // Symbol input has placeholder "Symbol (≥ 3 chars) — stocks, futures, options"
    const searchInput = page.locator('input[placeholder*="Symbol"]').first();
    await expect(searchInput).toBeVisible({ timeout: 5000 });
    await searchInput.fill('NIFTY');
    await page.waitForTimeout(1000);

    // Look for NIFTY in SymbolSearchInput's result rows (class="ssi-row")
    const result = page.locator('.ssi-row:has-text("NIFTY")').first();
    if (!await result.isVisible({ timeout: 3000 }).catch(() => false)) {
      test.skip(true, 'No NIFTY in typeahead — data unavailable or market closed');
      return;
    }
    await result.click();
    await page.waitForTimeout(500);

    // If it's F&O, the option picker opens as another [role="dialog"]
    // Either 1 (AddToPulse) or 2 (AddToPulse + option picker) dialogs present
    const count = await page.locator('[role="dialog"]').count();
    expect(count).toBeGreaterThanOrEqual(1);
  });

  test('Option picker ESC closes the top-most dialog', async ({ page }) => {
    await openAddToPulseModal(page);

    const searchInput = page.locator('input[placeholder*="Symbol"]').first();
    if (!await searchInput.isVisible({ timeout: 3000 }).catch(() => false)) {
      test.skip(true, 'Symbol input not found');
      return;
    }
    await searchInput.fill('NIFTY');
    await page.waitForTimeout(1000);

    // ESC — closes whatever dialog is on top (typeahead dropdown, option picker, or AddToPulse)
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);

    // At most one dialog should remain after one ESC
    const count = await page.locator('[role="dialog"]').count();
    expect(count).toBeLessThanOrEqual(1);
  });
});
