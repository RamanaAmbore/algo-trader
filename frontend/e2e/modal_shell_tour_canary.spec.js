import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

// Helper to set up auth and navigate to /showcase
async function setupShowcase(page) {
  await loginAsAdmin(page);
  await page.goto('/showcase');
  await page.waitForLoadState('networkidle');
}

test.describe('ModalShell + TourModal migration canary', () => {
  test('TourModal opens via "Take the tour" button', async ({ page }) => {
    await setupShowcase(page);

    // Find and click the "Take the 60-second tour" CTA button
    const tourButton = page.getByRole('button', { name: /take the.*tour/i });
    await expect(tourButton).toBeVisible();
    await tourButton.click();

    // Assert the tour modal overlay appears with proper a11y attributes
    const dialog = page.locator('[role="dialog"][aria-modal="true"]').filter({
      has: page.locator('.tour-modal')
    });
    await expect(dialog).toBeVisible();

    // Assert the tour title is visible
    const tourTitle = page.locator('#tour-title');
    await expect(tourTitle).toBeVisible();
    await expect(tourTitle).toContainText(/Two-process broker layer/i);
  });

  test('Esc key closes TourModal', async ({ page }) => {
    await setupShowcase(page);

    // Open the tour modal
    await page.getByRole('button', { name: /take the.*tour/i }).click();
    const dialog = page.locator('[role="dialog"][aria-modal="true"]').filter({
      has: page.locator('.tour-modal')
    });
    await expect(dialog).toBeVisible();

    // Press Escape
    await page.keyboard.press('Escape');

    // Assert modal is no longer visible
    await expect(dialog).not.toBeVisible();
  });

  test('Click outside (backdrop) closes TourModal', async ({ page }) => {
    await setupShowcase(page);

    // Open the tour modal
    await page.getByRole('button', { name: /take the.*tour/i }).click();
    const dialog = page.locator('[role="dialog"][aria-modal="true"]').filter({
      has: page.locator('.tour-modal')
    });
    await expect(dialog).toBeVisible();

    // Click the backdrop (overlay background)
    const overlay = page.locator('.ms-overlay');
    await overlay.click({ position: { x: 10, y: 10 } });

    // Assert modal is no longer visible
    await expect(dialog).not.toBeVisible();
  });

  test('Tour panel content is preserved', async ({ page }) => {
    await setupShowcase(page);

    // Open the tour modal
    await page.getByRole('button', { name: /take the.*tour/i }).click();

    // Assert tour step metadata is visible
    const stepTag = page.locator('.tour-tag');
    await expect(stepTag).toBeVisible();
    await expect(stepTag).toContainText(/broker isolation/i);

    // Assert step counter is visible
    const stepCount = page.locator('.tour-step-count');
    await expect(stepCount).toBeVisible();
    await expect(stepCount).toContainText(/1 \//);

    // Assert body content is visible
    const tourBody = page.locator('.tour-body');
    await expect(tourBody).toBeVisible();
    await expect(tourBody).toContainText(/broker sessions/i);

    // Assert navigation controls are visible
    const nextBtn = page.getByRole('button', { name: /Next →/i });
    await expect(nextBtn).toBeVisible();

    const pauseBtn = page.getByRole('button', { name: /Pause/i });
    await expect(pauseBtn).toBeVisible();
  });

  test('Tour modal fits in desktop viewport (1400×900)', async ({ page }) => {
    await setupShowcase(page);

    // Open the tour modal
    await page.getByRole('button', { name: /take the.*tour/i }).click();

    const tourModal = page.locator('.tour-modal');
    await expect(tourModal).toBeVisible();

    // Get the bounding box
    const bbox = await tourModal.boundingBox();
    expect(bbox).not.toBeNull();

    if (bbox) {
      // Assert the panel width stays within reasonable bounds (allowing margins)
      expect(bbox.width).toBeLessThan(600);
      // Assert it doesn't overflow the viewport height
      expect(bbox.height).toBeLessThan(900);
    }
  });

  test('Tour modal fits in mobile portrait viewport (360×800)', async ({ page }) => {
    // Set mobile portrait viewport
    await page.setViewportSize({ width: 360, height: 800 });

    await setupShowcase(page);

    // Open the tour modal
    await page.getByRole('button', { name: /take the.*tour/i }).click();

    const tourModal = page.locator('.tour-modal');
    await expect(tourModal).toBeVisible();

    // Get the bounding box
    const bbox = await tourModal.boundingBox();
    expect(bbox).not.toBeNull();

    if (bbox) {
      // Assert the panel width fits within the viewport (360px viewport means tight margins)
      // The modal has max-width: 36rem (576px) but should use media query on mobile
      expect(bbox.width).toBeLessThan(360);
      // Assert it doesn't overflow the viewport height
      expect(bbox.height).toBeLessThan(800);
    }

    // Assert no horizontal scrollbar on the modal
    const html = page.locator('html');
    const scrollWidth = await html.evaluate(el => el.scrollWidth);
    const clientWidth = await html.evaluate(el => el.clientWidth);
    expect(scrollWidth).toBeLessThanOrEqual(clientWidth);
  });

  test('Close button (×) closes the modal', async ({ page }) => {
    await setupShowcase(page);

    // Open the tour modal
    await page.getByRole('button', { name: /take the.*tour/i }).click();
    const dialog = page.locator('[role="dialog"][aria-modal="true"]').filter({
      has: page.locator('.tour-modal')
    });
    await expect(dialog).toBeVisible();

    // Click the close button (×)
    const closeBtn = page.getByRole('button', { name: /Close tour/i });
    await expect(closeBtn).toBeVisible();
    await closeBtn.click();

    // Assert modal is no longer visible
    await expect(dialog).not.toBeVisible();
  });

  test('Tour navigation works (Next button)', async ({ page }) => {
    await setupShowcase(page);

    // Open the tour modal
    await page.getByRole('button', { name: /take the.*tour/i }).click();

    // Assert we're on step 1
    const stepCount = page.locator('.tour-step-count');
    await expect(stepCount).toContainText(/1 \//);

    const tourTitle = page.locator('#tour-title');
    const firstTitle = await tourTitle.textContent();

    // Click Next
    const nextBtn = page.getByRole('button', { name: /Next →/i });
    await nextBtn.click();

    // Assert we're on step 2 (step counter changed)
    await expect(stepCount).toContainText(/2 \//);

    // Assert the title changed
    const secondTitle = await tourTitle.textContent();
    expect(firstTitle).not.toBe(secondTitle);
  });

  test('Tour progress bar is visible', async ({ page }) => {
    await setupShowcase(page);

    // Open the tour modal
    await page.getByRole('button', { name: /take the.*tour/i }).click();

    // Assert progress bar exists
    const progressBar = page.locator('.tour-progress-bar');
    await expect(progressBar).toBeVisible();

    // Assert it's animating (width should be > 0 after a short delay)
    await page.waitForTimeout(500);
    const width = await progressBar.evaluate(el => {
      const computed = window.getComputedStyle(el);
      return computed.width;
    });
    // Width should be a numeric value (not "0px" after 500ms of a 12s animation)
    expect(parseInt(width)).toBeGreaterThan(0);
  });
});
