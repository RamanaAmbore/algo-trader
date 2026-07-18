/**
 * modal-add-to-pulse.spec.js — AddToPulseModal (migrated to ModalShell) behavior.
 *
 * Trigger: /pulse → button[aria-label="Manage watchlists"] (mp-add-btn pencil icon).
 * After ModalShell migration the overlay is rendered by ModalShell (role="dialog").
 *
 * Verifies: opens with role=dialog, ESC closes, × closes, backdrop closes.
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

test.describe('AddToPulseModal — open/close/ESC/backdrop', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  async function openModal(page) {
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    // Wait for MarketPulse layout — don't use networkidle (SSE pages)
    await expect(page.locator('.mp-layout, .mp-bucket-wrap').first()).toBeVisible({ timeout: 20000 });
    // The "Manage watchlists" button is the pencil icon on the left panel
    const manageBtn = page.locator('button[aria-label="Manage watchlists"], button.mp-add-btn').first();
    await expect(manageBtn).toBeVisible({ timeout: 10000 });
    await manageBtn.click();
    const modal = page.locator('[role="dialog"]').first();
    await expect(modal).toBeVisible({ timeout: 5000 });
    return modal;
  }

  test('AddToPulseModal opens with role=dialog and aria-modal', async ({ page }) => {
    const modal = await openModal(page);
    await expect(modal).toHaveAttribute('aria-modal', 'true');
    await expect(page.locator('.search-title')).toBeVisible();
  });

  test('AddToPulseModal closes on ESC key press', async ({ page }) => {
    const modal = await openModal(page);
    await page.keyboard.press('Escape');
    await expect(modal).not.toBeVisible({ timeout: 3000 });
  });

  test('AddToPulseModal closes on × close button click', async ({ page }) => {
    const modal = await openModal(page);
    const closeBtn = page.locator('.search-close, button[aria-label="Close"], button:has-text("×")').first();
    await expect(closeBtn).toBeVisible();
    await closeBtn.click();
    await expect(modal).not.toBeVisible({ timeout: 3000 });
  });

  test('AddToPulseModal closes on backdrop click', async ({ page }) => {
    const modal = await openModal(page);
    // Click top-left corner — outside the modal panel, on the backdrop
    await page.mouse.click(10, 10);
    await expect(modal).not.toBeVisible({ timeout: 3000 });
  });
});
