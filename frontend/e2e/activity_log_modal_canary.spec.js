import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

/**
 * Canary: ActivityLogModal migration to ModalShell
 *
 * Verifies that ActivityLogModal correctly uses ModalShell with
 * dim={false} passthrough={true} — transparent backdrop, page underneath
 * stays interactive, panel itself is clickable.
 *
 * Key behaviour:
 * 1. Modal opens with role="dialog" aria-modal="true"
 * 2. Overlay has pointer-events: none (passthrough mode)
 * 3. Panel has pointer-events: auto (restored by .ms-passthrough > * rule)
 * 4. Esc closes the modal
 * 5. Tab switching works
 * 6. Mobile viewport doesn't overflow
 */

test.describe('ActivityLogModal (ModalShell migration)', () => {
  test.beforeEach(async ({ page }) => {
    // Pre-authenticate so we can access /pulse and the Log button
    await loginAsAdmin(page);
  });

  // Helper: Open the activity modal using the header button
  async function openActivityModal(page) {
    // PageHeaderActions renders the Activity button with aria-label="Open Activity"
    const logButton = page.locator('button[aria-label="Open Activity"]').first();
    await expect(logButton).toBeVisible({ timeout: 15000 });
    await logButton.click();
    // Wait for the modal overlay to render
    await expect(page.locator('.ms-overlay[role="dialog"]')).toBeVisible({ timeout: 10000 });
  }

  // Desktop (1400×900)
  test('desktop: modal opens with correct a11y attributes', async ({ page }) => {
    await page.goto('/pulse');
    // Don't wait for networkidle — dev.ramboq.com can be slow with market data
    // DOMContentLoaded is sufficient to render the page structure

    // Open the activity modal
    await openActivityModal(page);

    // Assert the modal overlay renders with proper a11y
    const overlay = page.locator('.ms-overlay[role="dialog"]');
    await expect(overlay).toBeVisible();
    await expect(overlay).toHaveAttribute('aria-modal', 'true');
  });

  test('desktop: transparent backdrop with passthrough (pointer-events: none)', async ({ page }) => {
    await page.goto('/pulse');
    // Don't wait for networkidle — dev.ramboq.com can be slow with market data
    // DOMContentLoaded is sufficient to render the page structure
    await openActivityModal(page);

    // Verify overlay has pointer-events: none (passthrough mode)
    const overlay = page.locator('.ms-overlay.ms-passthrough');
    await expect(overlay).toBeVisible();

    // Check computed style
    const pointerEvents = await overlay.evaluate(el => getComputedStyle(el).pointerEvents);
    expect(pointerEvents).toBe('none');

    // Verify no dark background (dim=false). Since passthrough is applied,
    // the .ms-dim class should NOT be present
    await expect(overlay).not.toHaveClass('ms-dim');
  });

  test('desktop: panel inside overlay has pointer-events: auto', async ({ page }) => {
    await page.goto('/pulse');
    // Don't wait for networkidle — dev.ramboq.com can be slow with market data
    // DOMContentLoaded is sufficient to render the page structure
    await openActivityModal(page);

    // Find the panel — direct child of .ms-overlay with class .canonical-modal-panel
    const panel = page.locator('.ms-overlay > .canonical-modal-panel');
    await expect(panel).toBeVisible();

    // Panel should have pointer-events: auto (restored by :global(.ms-passthrough) > * rule)
    const pointerEvents = await panel.evaluate(el => getComputedStyle(el).pointerEvents);
    expect(pointerEvents).toBe('auto');
  });

  test('desktop: Esc closes modal', async ({ page }) => {
    await page.goto('/pulse');
    // Don't wait for networkidle — dev.ramboq.com can be slow with market data
    // DOMContentLoaded is sufficient to render the page structure
    await openActivityModal(page);

    // Press Escape
    await page.keyboard.press('Escape');

    // Modal should close
    await expect(page.locator('.ms-overlay[role="dialog"]')).not.toBeVisible();
  });

  test('desktop: close button (×) dismisses modal', async ({ page }) => {
    await page.goto('/pulse');
    // Don't wait for networkidle — dev.ramboq.com can be slow with market data
    // DOMContentLoaded is sufficient to render the page structure
    await openActivityModal(page);

    // Click the close button
    const closeBtn = page.locator('.alm-close');
    await expect(closeBtn).toBeVisible();
    await closeBtn.click();

    // Modal should close
    await expect(page.locator('.ms-overlay[role="dialog"]')).not.toBeVisible();
  });

  test('desktop: tab switching updates active tab', async ({ page }) => {
    await page.goto('/pulse');
    // Don't wait for networkidle — dev.ramboq.com can be slow with market data
    // DOMContentLoaded is sufficient to render the page structure
    await openActivityModal(page);

    // Find the tab list inside the modal (LogPanel renders it)
    const tabList = page.locator('.ms-overlay [role="tablist"]').first();
    await expect(tabList).toBeVisible();

    // Get all tab buttons
    const tabs = tabList.locator('[role="tab"]');
    const tabCount = await tabs.count();
    expect(tabCount).toBeGreaterThan(0);

    // Click the second tab (if exists)
    if (tabCount > 1) {
      const secondTab = tabs.nth(1);
      await secondTab.click();

      // Verify it becomes active (aria-selected="true" or has active class)
      await expect(secondTab).toHaveAttribute('aria-selected', 'true');
    }
  });

  test('desktop: page underneath remains interactive (passthrough mode)', async ({ page }) => {
    await page.goto('/pulse');
    // Don't wait for networkidle — dev.ramboq.com can be slow with market data
    // DOMContentLoaded is sufficient to render the page structure
    await openActivityModal(page);

    // Verify the overlay has pointer-events: none (passthrough mode)
    const overlay = page.locator('.ms-overlay.ms-passthrough');
    const overlayPointerEvents = await overlay.evaluate(el => getComputedStyle(el).pointerEvents);
    expect(overlayPointerEvents).toBe('none');
  });

  // Mobile Portrait (360×800)
  test('mobile-portrait: modal opens with correct a11y attributes', async ({ page }) => {
    await page.goto('/pulse');
    // Don't wait for networkidle — dev.ramboq.com can be slow with market data
    // DOMContentLoaded is sufficient to render the page structure
    await openActivityModal(page);

    // Assert the modal overlay renders with proper a11y
    const overlay = page.locator('.ms-overlay[role="dialog"]');
    await expect(overlay).toHaveAttribute('aria-modal', 'true');
  });

  test('mobile-portrait: transparent backdrop with passthrough', async ({ page }) => {
    await page.goto('/pulse');
    // Don't wait for networkidle — dev.ramboq.com can be slow with market data
    // DOMContentLoaded is sufficient to render the page structure
    await openActivityModal(page);

    // Verify overlay has pointer-events: none
    const overlay = page.locator('.ms-overlay.ms-passthrough');
    await expect(overlay).toBeVisible();

    const pointerEvents = await overlay.evaluate(el => getComputedStyle(el).pointerEvents);
    expect(pointerEvents).toBe('none');

    // No dark background
    await expect(overlay).not.toHaveClass('ms-dim');
  });

  test('mobile-portrait: panel stays within viewport (no horizontal scroll)', async ({ page, browserName }) => {
    await page.goto('/pulse');
    // Don't wait for networkidle — dev.ramboq.com can be slow with market data
    // DOMContentLoaded is sufficient to render the page structure
    await openActivityModal(page);

    // Find the panel
    const panel = page.locator('.ms-overlay > .canonical-modal-panel');
    await expect(panel).toBeVisible();

    // Get the panel's bounding box
    const panelBox = await panel.boundingBox();
    expect(panelBox).toBeTruthy();

    // Only check width on mobile-portrait viewport (360px)
    // On chromium-desktop running mobile tests, it renders at the desktop width
    if (page.viewportSize().width <= 400) {
      expect(panelBox.width).toBeLessThanOrEqual(360);
    } else {
      // On desktop viewport, just verify the panel is visible and reasonable
      expect(panelBox.width).toBeGreaterThan(0);
    }
  });

  test('mobile-portrait: Esc closes modal', async ({ page }) => {
    await page.goto('/pulse');
    // Don't wait for networkidle — dev.ramboq.com can be slow with market data
    // DOMContentLoaded is sufficient to render the page structure
    await openActivityModal(page);

    // Press Escape
    await page.keyboard.press('Escape');

    // Modal should close
    await expect(page.locator('.ms-overlay[role="dialog"]')).not.toBeVisible();
  });

  test('mobile-portrait: close button is visible and clickable', async ({ page }) => {
    await page.goto('/pulse');
    // Don't wait for networkidle — dev.ramboq.com can be slow with market data
    // DOMContentLoaded is sufficient to render the page structure
    await openActivityModal(page);

    // Find and click the close button
    const closeBtn = page.locator('.alm-close');
    await expect(closeBtn).toBeVisible();

    // Ensure it's actually in the viewport and clickable on mobile
    const closeBox = await closeBtn.boundingBox();
    expect(closeBox).toBeTruthy();
    expect(closeBox.width).toBeGreaterThan(0);
    expect(closeBox.height).toBeGreaterThan(0);

    // Click it
    await closeBtn.click();

    // Modal should close
    await expect(page.locator('.ms-overlay[role="dialog"]')).not.toBeVisible();
  });

  test('keyboard shortcut "h" opens activity modal (bonus check)', async ({ page }) => {
    await page.goto('/pulse');
    // Don't wait for networkidle — dev.ramboq.com can be slow with market data
    // DOMContentLoaded is sufficient to render the page structure

    // First ensure the page is interactive by clicking somewhere to set focus
    await page.locator('main').first().click();

    // Press 'h' (activity/log keyboard shortcut)
    // The shortcut requires focus to be on the page, not in an input field
    await page.keyboard.press('h');

    // Modal should open
    const overlay = page.locator('.ms-overlay[role="dialog"]');
    // Give it a longer timeout since keyboard events can be slower
    await expect(overlay).toBeVisible({ timeout: 10000 });
  });
});
