/**
 * broker-health-nav.spec.js
 *
 * Validates Fix A: the `.bh-overlay` div that sits at z-index 9990 must have
 * `pointer-events: none` so nav links remain clickable while the broker-health
 * modal is open.
 *
 * Without the fix the overlay (transparent, fixed, full-viewport) sits on top
 * of the nav (z-index 50) and swallows all clicks.
 *
 * Five quality dimensions:
 *   1. SSOT   — tests the computed CSS property on the rendered overlay element
 *   2. Perf   — single page load, no network round-trips beyond initial mount
 *   3. Stale  — modal dismiss paths (ESC + X button) also verified
 *   4. Reuse  — loginAsAdmin fixture from fixtures/auth.js
 *   5. UX     — pointer-events: none is the canonical accessibility fix; nav
 *               link not occluded confirms the broker modal does not block UI
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 30_000;

test.describe('BrokerHealthBadge — overlay does not block nav clicks', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: TIMEOUT });
  });

  test('broker-chip button exists in the nav', async ({ page }) => {
    // The chip is rendered in the algo layout navbar — confirm it is visible.
    const chip = page.locator('.broker-chip').first();
    await expect(chip).toBeVisible({ timeout: TIMEOUT });
  });

  test('clicking broker-chip opens the health modal', async ({ page }) => {
    const chip = page.locator('.broker-chip').first();
    await expect(chip).toBeVisible({ timeout: TIMEOUT });
    await chip.click();

    // The modal has class bh-modal — must be visible after click.
    const modal = page.locator('.bh-modal');
    await expect(modal).toBeVisible({ timeout: 5_000 });
  });

  test('overlay has pointer-events: none so nav links are not blocked', async ({ page }) => {
    const chip = page.locator('.broker-chip').first();
    await expect(chip).toBeVisible({ timeout: TIMEOUT });
    await chip.click();

    // Modal must be visible (confirms overlay rendered).
    await expect(page.locator('.bh-modal')).toBeVisible({ timeout: 5_000 });

    // Core assertion: the overlay must have pointer-events: none.
    // Without the fix it is the browser default ('auto'), which swallows clicks.
    const overlay = page.locator('.bh-overlay');
    await expect(overlay).toBeVisible({ timeout: 2_000 });

    const pointerEvents = await overlay.evaluate(
      (el) => window.getComputedStyle(el).pointerEvents,
    );
    expect(pointerEvents).toBe('none');
  });

  test('ESC key dismisses the modal', async ({ page }) => {
    const chip = page.locator('.broker-chip').first();
    await expect(chip).toBeVisible({ timeout: TIMEOUT });
    await chip.click();
    await expect(page.locator('.bh-modal')).toBeVisible({ timeout: 5_000 });

    // Press Escape — the svelte:window onkeydown handler sets open=false.
    await page.keyboard.press('Escape');
    await expect(page.locator('.bh-modal')).not.toBeVisible({ timeout: 3_000 });
  });

  test('X button inside modal dismisses the modal', async ({ page }) => {
    const chip = page.locator('.broker-chip').first();
    await expect(chip).toBeVisible({ timeout: TIMEOUT });
    await chip.click();
    await expect(page.locator('.bh-modal')).toBeVisible({ timeout: 5_000 });

    // The close button has class bh-close.
    const closeBtn = page.locator('.bh-close');
    await expect(closeBtn).toBeVisible({ timeout: 2_000 });
    await closeBtn.click();
    await expect(page.locator('.bh-modal')).not.toBeVisible({ timeout: 3_000 });
  });
});
