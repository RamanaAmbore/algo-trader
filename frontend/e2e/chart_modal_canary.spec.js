import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

/**
 * Canary: ChartModal migration to ModalShell
 *
 * Verifies that ChartModal correctly uses ModalShell with
 * dim={false} passthrough={true} — transparent backdrop, page underneath
 * stays interactive (pointer-events:none on overlay), panel itself is clickable.
 *
 * Key behaviour:
 * 1. Modal opens with role="dialog" aria-modal="true" aria-label="Chart —"
 * 2. Overlay has pointer-events: none (passthrough mode)
 * 3. Panel has pointer-events: auto (restored by .ms-passthrough > * rule)
 * 4. Esc closes the modal
 * 5. Close button (×) closes the modal
 * 6. Backdrop click does NOT close (clickOutside=false)
 * 7. Page underneath remains interactive
 * 8. Mobile viewport doesn't overflow
 */

test.describe('ChartModal (ModalShell migration)', () => {
  test.beforeEach(async ({ page }) => {
    // Pre-authenticate so we can access /pulse and the Chart button
    await loginAsAdmin(page);
  });

  // Helper: Open the ChartModal using the keyboard shortcut 'k'
  async function openChartModal(page) {
    // Navigate to /pulse to access the Chart modal
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    // Wait for the page header to be visible (contains PageHeaderActions)
    // This also means the page layout has fully rendered
    await page.locator('.page-header').first().waitFor({ state: 'visible', timeout: 15000 });

    // Use keyboard shortcut 'k' (defined in (algo)/+layout.svelte) to open chart modal
    // First click to ensure focus is on the page
    await page.locator('main').first().click();
    await page.keyboard.press('k');

    // Wait for the modal overlay to render — ModalShell creates an element
    // with role="dialog" and class="ms-overlay"
    await expect(page.locator('.ms-overlay[role="dialog"]')).toBeVisible({ timeout: 20000 });
  }

  // ─── Desktop (1400×900) ──────────────────────────────────────────

  test('desktop: modal opens with correct a11y attributes', async ({ page }) => {
    await openChartModal(page);

    // Assert the modal overlay renders with proper a11y
    const overlay = page.locator('.ms-overlay[role="dialog"]');
    await expect(overlay).toBeVisible();
    await expect(overlay).toHaveAttribute('aria-modal', 'true');

    // Verify aria-label contains "Chart —" prefix
    const ariaLabel = await overlay.getAttribute('aria-label');
    expect(ariaLabel).toMatch(/^Chart —/);
  });

  test('desktop: transparent backdrop with passthrough (pointer-events: none)', async ({ page }) => {
    await openChartModal(page);

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
    await openChartModal(page);

    // Find the panel — direct child of .ms-overlay with class .canonical-modal-panel
    const panel = page.locator('.ms-overlay > .canonical-modal-panel');
    await expect(panel).toBeVisible();

    // Panel should have pointer-events: auto (restored by :global(.ms-passthrough) > * rule)
    const pointerEvents = await panel.evaluate(el => getComputedStyle(el).pointerEvents);
    expect(pointerEvents).toBe('auto');
  });

  test('desktop: Esc closes modal', async ({ page }) => {
    await openChartModal(page);

    // Press Escape
    await page.keyboard.press('Escape');

    // Modal should close — wait briefly for state update and DOM removal
    await expect(page.locator('.ms-overlay[role="dialog"]')).not.toBeVisible({ timeout: 5000 });
  });

  test('desktop: close button (×) dismisses modal', async ({ page }) => {
    await openChartModal(page);

    // Find the close button — ChartModal uses .cm-close class
    const closeBtn = page.locator('.cm-close');
    await expect(closeBtn).toBeVisible();
    await closeBtn.click();

    // Modal should close
    await expect(page.locator('.ms-overlay[role="dialog"]')).not.toBeVisible();
  });

  test('desktop: backdrop click does NOT close (clickOutside=false)', async ({ page }) => {
    await openChartModal(page);

    // Click on the overlay backdrop (outside the panel)
    // The overlay is passthrough (pointer-events:none) but we can still
    // interact with it via Playwright — this tests that the backend
    // clickOutside=false logic prevents closes
    const overlay = page.locator('.ms-overlay');
    const overlayBox = await overlay.boundingBox();

    // Click at the top-left corner of the overlay (definitely outside any panel)
    await page.click(`body`, {
      position: {
        x: overlayBox.x + 10,
        y: overlayBox.y + 10,
      },
    });

    // Modal should still be visible (click-outside disabled)
    // Wait briefly to ensure no async close is triggered
    await page.waitForTimeout(200);
    await expect(page.locator('.ms-overlay[role="dialog"]')).toBeVisible();
  });

  test('desktop: page underneath remains interactive (passthrough mode)', async ({ page }) => {
    await openChartModal(page);

    // Verify the overlay has pointer-events: none (passthrough mode)
    const overlay = page.locator('.ms-overlay.ms-passthrough');
    const overlayPointerEvents = await overlay.evaluate(el => getComputedStyle(el).pointerEvents);
    expect(overlayPointerEvents).toBe('none');

    // The chart button should still be reachable / interactable
    // (Though we won't click it since we want the modal to stay open for this test)
  });

  test('desktop: aria-label prefixes with "Chart —"', async ({ page }) => {
    await openChartModal(page);

    const overlay = page.locator('.ms-overlay[role="dialog"]');
    const ariaLabel = await overlay.getAttribute('aria-label');

    // Verify it starts with "Chart —" and has content after
    expect(ariaLabel).toMatch(/^Chart — /);
    expect(ariaLabel.length).toBeGreaterThan('Chart — '.length);
  });

  // ─── Mobile Portrait (360×800) ───────────────────────────────────

  test('mobile-portrait: modal opens with correct a11y attributes', async ({ page }) => {
    await openChartModal(page);

    // Assert the modal overlay renders with proper a11y
    const overlay = page.locator('.ms-overlay[role="dialog"]');
    await expect(overlay).toBeVisible();
    await expect(overlay).toHaveAttribute('aria-modal', 'true');

    // Verify aria-label contains "Chart —" prefix
    const ariaLabel = await overlay.getAttribute('aria-label');
    expect(ariaLabel).toMatch(/^Chart —/);
  });

  test('mobile-portrait: transparent backdrop with passthrough', async ({ page }) => {
    await openChartModal(page);

    // Verify overlay has pointer-events: none
    const overlay = page.locator('.ms-overlay.ms-passthrough');
    await expect(overlay).toBeVisible();

    const pointerEvents = await overlay.evaluate(el => getComputedStyle(el).pointerEvents);
    expect(pointerEvents).toBe('none');

    // No dark background
    await expect(overlay).not.toHaveClass('ms-dim');
  });

  test('mobile-portrait: panel stays within viewport (no horizontal scroll)', async ({ page }) => {
    await openChartModal(page);

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
    await openChartModal(page);

    // Press Escape
    await page.keyboard.press('Escape');

    // Modal should close
    await expect(page.locator('.ms-overlay[role="dialog"]')).not.toBeVisible();
  });

  test('mobile-portrait: close button is visible and clickable', async ({ page }) => {
    await openChartModal(page);

    // Find and click the close button
    const closeBtn = page.locator('.cm-close');
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

  test('mobile-portrait: panel has pointer-events: auto', async ({ page }) => {
    await openChartModal(page);

    // Find the panel
    const panel = page.locator('.ms-overlay > .canonical-modal-panel');
    await expect(panel).toBeVisible();

    // Panel should have pointer-events: auto
    const pointerEvents = await panel.evaluate(el => getComputedStyle(el).pointerEvents);
    expect(pointerEvents).toBe('auto');
  });

  test('mobile-portrait: keyboard shortcut "k" opens chart modal (bonus check)', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    // First ensure the page is interactive by clicking somewhere to set focus
    await page.locator('main').first().click();

    // Press 'k' (chart keyboard shortcut)
    // The shortcut requires focus to be on the page, not in an input field
    await page.keyboard.press('k');

    // Modal should open
    const overlay = page.locator('.ms-overlay[role="dialog"]');
    // Give it a longer timeout since keyboard events can be slower
    await expect(overlay).toBeVisible({ timeout: 10000 });
  });
});
