/**
 * modal-confirm.spec.js — ConfirmModal (migrated to ModalShell) behavior.
 *
 * Trigger: /strategies → × delete button on any strategy row.
 * ConfirmModal renders role="dialog" via ModalShell after migration.
 *
 * All tests dismiss without confirming — no data is modified.
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

test.describe('ConfirmModal — open/close/ESC/backdrop', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  async function openConfirm(page) {
    await page.goto(`${BASE}/strategies`, { waitUntil: 'domcontentloaded' });
    // Wait for strategy rows to appear — don't use networkidle (SSE pages)
    await expect(page.locator('table, [class*="strategy"], .strat-row').first()).toBeVisible({ timeout: 20000 });
    // Click first × delete button (only non-system strategies have one)
    const deleteBtn = page.locator('button.btn-danger, button:has-text("×")').first();
    if (!await deleteBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
      return null; // No deletable strategies
    }
    await deleteBtn.click();
    const modal = page.locator('[role="dialog"]').first();
    await expect(modal).toBeVisible({ timeout: 5000 });
    return modal;
  }

  test('ConfirmModal opens on delete click with role=dialog', async ({ page }) => {
    const modal = await openConfirm(page);
    if (!modal) { test.skip(true, 'No deletable strategy on this server'); return; }
    await expect(modal).toHaveAttribute('aria-modal', 'true');
  });

  test('ConfirmModal closes on ESC key press', async ({ page }) => {
    const modal = await openConfirm(page);
    if (!modal) { test.skip(true, 'No deletable strategy on this server'); return; }
    await page.keyboard.press('Escape');
    await expect(modal).not.toBeVisible({ timeout: 3000 });
  });

  test('ConfirmModal closes on Cancel button click', async ({ page }) => {
    const modal = await openConfirm(page);
    if (!modal) { test.skip(true, 'No deletable strategy on this server'); return; }
    const cancelBtn = page.locator('.cm-cancel, button:has-text("Cancel"), button:has-text("No")').first();
    await expect(cancelBtn).toBeVisible();
    await cancelBtn.click();
    await expect(modal).not.toBeVisible({ timeout: 3000 });
  });

  test('ConfirmModal closes on backdrop click', async ({ page }) => {
    const modal = await openConfirm(page);
    if (!modal) { test.skip(true, 'No deletable strategy on this server'); return; }
    // Click top-left corner — outside the modal panel, on the backdrop
    await page.mouse.click(10, 10);
    await expect(modal).not.toBeVisible({ timeout: 3000 });
  });
});
