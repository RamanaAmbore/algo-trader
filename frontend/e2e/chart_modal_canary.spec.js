import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

/**
 * Canary: ChartModal own-overlay (portal) implementation.
 *
 * ChartModal uses its own `use:portal` overlay div (.canonical-modal-overlay)
 * rather than ModalShell, because its requirements fight ModalShell:
 *   - capture-phase Esc with stopImmediatePropagation (blocks SymbolPanel)
 *   - pointer-events:none overlay with native addEventListener for × close
 *   - Tab focus trap scoped to _modalEl
 *
 * Key behaviour:
 * 1. Modal opens with role="dialog" aria-modal="true" aria-label="Chart —"
 * 2. Overlay (.canonical-modal-overlay) is pointer-events:none
 * 3. Panel (.canonical-modal-panel) is visible and interactive
 * 4. Esc closes the modal
 * 5. Close button (×) closes the modal
 * 6. Page underneath remains interactive (passthrough overlay)
 * 7. Mobile viewport doesn't overflow
 */

test.describe('ChartModal (own-portal overlay)', () => {
  test.beforeEach(async ({ page }) => {
    // Pre-authenticate so we can access /pulse and the Chart button
    await loginAsAdmin(page);
  });

  // Helper: Open the ChartModal using the keyboard shortcut 'k'
  async function openChartModal(page) {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.locator('.page-header').first().waitFor({ state: 'visible', timeout: 15000 });
    await page.locator('main').first().click();
    await page.keyboard.press('k');
    // ChartModal uses .canonical-modal-overlay with role="dialog"
    await expect(page.locator('.canonical-modal-overlay[role="dialog"]')).toBeVisible({ timeout: 20000 });
  }

  // ─── Desktop (1400×900) ──────────────────────────────────────────

  test('desktop: modal opens with correct a11y attributes', async ({ page }) => {
    await openChartModal(page);

    const overlay = page.locator('.canonical-modal-overlay[role="dialog"]');
    await expect(overlay).toBeVisible();
    await expect(overlay).toHaveAttribute('aria-modal', 'true');

    const ariaLabel = await overlay.getAttribute('aria-label');
    expect(ariaLabel).toMatch(/^Chart —/);
  });

  test('desktop: overlay has pointer-events: none (passthrough)', async ({ page }) => {
    await openChartModal(page);

    const overlay = page.locator('.canonical-modal-overlay');
    await expect(overlay).toBeVisible();

    const pointerEvents = await overlay.evaluate(el => getComputedStyle(el).pointerEvents);
    expect(pointerEvents).toBe('none');
  });

  test('desktop: panel inside overlay is visible', async ({ page }) => {
    await openChartModal(page);

    const panel = page.locator('.canonical-modal-overlay > .canonical-modal-panel');
    await expect(panel).toBeVisible();
  });

  test('desktop: Esc closes modal', async ({ page }) => {
    await openChartModal(page);

    await page.keyboard.press('Escape');
    await expect(page.locator('.canonical-modal-overlay[role="dialog"]')).not.toBeVisible({ timeout: 5000 });
  });

  test('desktop: close button (×) dismisses modal', async ({ page }) => {
    await openChartModal(page);

    const closeBtn = page.locator('.cm-close');
    await expect(closeBtn).toBeVisible();
    await closeBtn.click();

    await expect(page.locator('.canonical-modal-overlay[role="dialog"]')).not.toBeVisible();
  });

  test('desktop: aria-label prefixes with "Chart —"', async ({ page }) => {
    await openChartModal(page);

    const overlay = page.locator('.canonical-modal-overlay[role="dialog"]');
    const ariaLabel = await overlay.getAttribute('aria-label');
    expect(ariaLabel).toMatch(/^Chart —/);
  });

  // ─── Mobile Portrait (360×800) ───────────────────────────────────

  test('mobile-portrait: modal opens with correct a11y attributes', async ({ page }) => {
    await openChartModal(page);

    const overlay = page.locator('.canonical-modal-overlay[role="dialog"]');
    await expect(overlay).toBeVisible();
    await expect(overlay).toHaveAttribute('aria-modal', 'true');

    const ariaLabel = await overlay.getAttribute('aria-label');
    expect(ariaLabel).toMatch(/^Chart —/);
  });

  test('mobile-portrait: overlay has pointer-events: none', async ({ page }) => {
    await openChartModal(page);

    const overlay = page.locator('.canonical-modal-overlay');
    await expect(overlay).toBeVisible();

    const pointerEvents = await overlay.evaluate(el => getComputedStyle(el).pointerEvents);
    expect(pointerEvents).toBe('none');
  });

  test('mobile-portrait: panel stays within viewport', async ({ page }) => {
    await openChartModal(page);

    const panel = page.locator('.canonical-modal-overlay > .canonical-modal-panel');
    await expect(panel).toBeVisible();

    const panelBox = await panel.boundingBox();
    expect(panelBox).toBeTruthy();

    if (page.viewportSize().width <= 400) {
      expect(panelBox.width).toBeLessThanOrEqual(360);
    } else {
      expect(panelBox.width).toBeGreaterThan(0);
    }
  });

  test('mobile-portrait: Esc closes modal', async ({ page }) => {
    await openChartModal(page);

    await page.keyboard.press('Escape');
    await expect(page.locator('.canonical-modal-overlay[role="dialog"]')).not.toBeVisible();
  });

  test('mobile-portrait: close button is visible and clickable', async ({ page }) => {
    await openChartModal(page);

    const closeBtn = page.locator('.cm-close');
    await expect(closeBtn).toBeVisible();

    const closeBox = await closeBtn.boundingBox();
    expect(closeBox).toBeTruthy();
    expect(closeBox.width).toBeGreaterThan(0);
    expect(closeBox.height).toBeGreaterThan(0);

    await closeBtn.click();
    await expect(page.locator('.canonical-modal-overlay[role="dialog"]')).not.toBeVisible();
  });

  test('mobile-portrait: panel has pointer-events: auto', async ({ page }) => {
    await openChartModal(page);

    const panel = page.locator('.canonical-modal-overlay > .canonical-modal-panel');
    await expect(panel).toBeVisible();

    const pointerEvents = await panel.evaluate(el => getComputedStyle(el).pointerEvents);
    expect(pointerEvents).toBe('auto');
  });

  test('mobile-portrait: keyboard shortcut "k" opens chart modal', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.locator('main').first().click();
    await page.keyboard.press('k');

    const overlay = page.locator('.canonical-modal-overlay[role="dialog"]');
    await expect(overlay).toBeVisible({ timeout: 10000 });
  });
});
