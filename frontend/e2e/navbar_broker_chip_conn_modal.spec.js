// Verify the navbar broker-status chip opens ActivityLogModal with the
// Conn tab pre-selected (commit f9308a62 — activityModal store + layout wiring).
//
// Target: https://dev.ramboq.com only (NOT prod).
//   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com npx playwright test e2e/navbar_broker_chip_conn_modal.spec.js --project=chromium-desktop
//
// KNOWN BUG (as of initial run against dev.ramboq.com):
//   Opening the ActivityLogModal via the broker chip triggers a Svelte
//   `effect_update_depth_exceeded` pageerror. This kills the reactivity loop
//   that would remove the {#if $activityModal.open} block, so the × close
//   button and Esc both call closeActivityModal() successfully (stopPropagation
//   fires) but the store update never propagates to re-render the layout.
//   Tests 1–3 (chip visible + modal opens + Conn tab active) pass.
//   Test 4 (close) fails until the reactivity loop is fixed.

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

test.describe('navbar broker chip → ActivityLogModal Conn tab', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('chip is visible in the navbar', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    // broker-chip appears once the connStatus poller fires (~2s after mount).
    // The chip class is always "broker-chip" plus a state modifier
    // (broker-chip-ok / broker-chip-partial / broker-chip-down / broker-chip-unknown).
    const chip = page.locator('button.broker-chip').first();
    await expect(chip, 'broker-chip button visible in the navbar').toBeVisible({ timeout: 25_000 });
  });

  test('clicking the chip opens ActivityLogModal with Conn tab active', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    const chip = page.locator('button.broker-chip').first();
    await expect(chip).toBeVisible({ timeout: 25_000 });

    // broker-chip-partial and broker-chip-down carry a CSS pulse animation
    // (algo-mode-dot keyframe) that keeps the element "not stable" by
    // Playwright's actionability definition. force:true bypasses that check;
    // the button is visible, enabled, and pointer-events:auto.
    await chip.click({ force: true });

    // Modal overlay is teleported to <body> via the portal action.
    const overlay = page.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(overlay, 'ActivityLogModal overlay appears').toBeVisible({ timeout: 8_000 });

    // AlgoTabs renders tab buttons with role="tab" and aria-selected={active}.
    // openActivityModal('conn') seeds defaultTab='conn' on LogPanel so the
    // Conn tab must be aria-selected="true" immediately on open.
    const connTab = overlay.locator('[role="tab"]:has-text("Conn")');
    await expect(connTab, 'Conn tab is rendered').toBeVisible();
    await expect(connTab, 'Conn tab is aria-selected=true (active)').toHaveAttribute('aria-selected', 'true');
  });

  test('× close button dismisses the overlay', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    const chip = page.locator('button.broker-chip').first();
    await expect(chip).toBeVisible({ timeout: 25_000 });
    await chip.click({ force: true });

    const overlay = page.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(overlay).toBeVisible({ timeout: 8_000 });

    // The close button has pointer-events:auto even though the overlay is
    // pointer-events:none (only the panel inside is interactive).
    await page.locator('[aria-label="Close activity log"]').click({ force: true });
    await expect(overlay, 'overlay hidden after × click').not.toBeVisible({ timeout: 5_000 });
  });

  test('Esc key dismisses the overlay', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    const chip = page.locator('button.broker-chip').first();
    await expect(chip).toBeVisible({ timeout: 25_000 });
    await chip.click({ force: true });

    const overlay = page.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(overlay).toBeVisible({ timeout: 8_000 });

    // ActivityLogModal registers window.addEventListener('keydown', handler,
    // {capture:true}) so Escape reaches the handler regardless of focus.
    await page.keyboard.press('Escape');
    await expect(overlay, 'overlay dismissed by Escape').not.toBeVisible({ timeout: 5_000 });
  });
});
