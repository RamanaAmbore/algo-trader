import { test, expect } from '@playwright/test';
import { loginAsAdmin, visitAnonymous } from './fixtures/auth.js';

/**
 * Guard spec: MarketPulse "Add to watchlist" button ("+")
 *
 * Verifies that:
 * 1. The "+" button appears in the Pinned/Watchlist card header when logged in (!isDemo)
 * 2. The button has text content "+" (catches SVG-replacement regression)
 * 3. Clicking the button opens the AddToPulseModal
 * 4. The button is NOT shown in demo mode (anonymous visitor on prod)
 *
 * Regression guard: Previously the button was replaced with a pencil SVG instead
 * of keeping the "+" text. This test ensures the text "+" is always rendered.
 */

test.describe('MarketPulse add-to-watchlist button guard', () => {
  test.use({ baseURL: 'https://dev.ramboq.com' });

  test('button is visible and shows "+" text when logged in', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse');
    await page.waitForLoadState('networkidle');

    // Find the button by aria-label
    const addButton = page.locator('button[aria-label="Add to watchlist"]');

    // Verify button is visible
    await expect(addButton).toBeVisible({ timeout: 5000 });

    // Verify the button text content is "+"
    // (catches regression where button showed SVG instead of "+")
    const textContent = await addButton.textContent();
    expect(textContent).toBe('+');
  });

  test('button has correct title attribute', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse');
    await page.waitForLoadState('networkidle');

    const addButton = page.locator('button[aria-label="Add to watchlist"]');
    await expect(addButton).toBeVisible({ timeout: 5000 });

    // Verify title hint (keyboard shortcut)
    const title = await addButton.getAttribute('title');
    expect(title).toBe('Add to watchlist (/)');
  });

  test('clicking button opens the AddToPulseModal', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse');
    await page.waitForLoadState('networkidle');

    // Click the add-to-watchlist button
    const addButton = page.locator('button[aria-label="Add to watchlist"]');
    await addButton.click();

    // Modal should now be visible
    // AddToPulseModal has role="dialog" and aria-label contains "Add to Pulse"
    const modal = page.locator('[role="dialog"][aria-label*="Add to Pulse"]');
    await expect(modal).toBeVisible({ timeout: 3000 });
  });

  test('modal contains symbol input field after button click', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse');
    await page.waitForLoadState('networkidle');

    const addButton = page.locator('button[aria-label="Add to watchlist"]');
    await addButton.click();

    // Modal should open and contain an input for symbol search
    const modal = page.locator('[role="dialog"][aria-label*="Add to Pulse"]');
    await expect(modal).toBeVisible({ timeout: 3000 });

    // Look for a symbol/searchable input field
    // The modal typically has a searchable input for adding symbols
    const symbolInput = modal.locator('input[type="text"], input[placeholder*="symbol" i], input[placeholder*="search" i]').first();
    await expect(symbolInput).toBeVisible();
  });

  test('button is hidden in demo mode (anonymous visitor)', async ({ page }) => {
    // Visit as anonymous (no token)
    await visitAnonymous(page);
    await page.goto('/pulse');
    await page.waitForLoadState('networkidle');

    // The button should NOT be rendered when isDemo is true
    const addButton = page.locator('button[aria-label="Add to watchlist"]');
    const isVisible = await addButton.isVisible().catch(() => false);

    // In demo mode, button is conditionally rendered with {#if !isDemo},
    // so it should not exist in the DOM
    expect(isVisible).toBe(false);
  });

  test('button is accessible via keyboard shortcut (/)', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse');
    await page.waitForLoadState('networkidle');

    // Verify the button has the correct title hint about the shortcut
    const addButton = page.locator('button[aria-label="Add to watchlist"]');
    const title = await addButton.getAttribute('title');
    expect(title).toContain('/');

    // This test verifies the affordance is in place; the actual shortcut
    // wiring is tested elsewhere. We're just guarding the button exists
    // and has the hint.
  });

  test('button closes when modal is dismissed', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse');
    await page.waitForLoadState('networkidle');

    // Click to open modal
    const addButton = page.locator('button[aria-label="Add to watchlist"]');
    await addButton.click();

    const modal = page.locator('[role="dialog"][aria-label*="Add to Pulse"]');
    await expect(modal).toBeVisible({ timeout: 3000 });

    // Close the modal by pressing Escape
    await page.keyboard.press('Escape');

    // Modal should now be hidden
    await expect(modal).toBeHidden({ timeout: 2000 });

    // Button should still be visible and clickable
    await expect(addButton).toBeVisible();
  });

  test('button shows correct button class for styling', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse');
    await page.waitForLoadState('networkidle');

    const addButton = page.locator('button[aria-label="Add to watchlist"]');
    await expect(addButton).toBeVisible({ timeout: 5000 });

    // Verify it has the correct class for styling (mp-add-btn)
    const hasClass = await addButton.evaluate((el) => el.classList.contains('mp-add-btn'));
    expect(hasClass).toBe(true);
  });

  test('button is in Pinned/Watchlist card header, not in grid rows', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse');
    await page.waitForLoadState('networkidle');

    const addButton = page.locator('button[aria-label="Add to watchlist"]');
    await expect(addButton).toBeVisible({ timeout: 5000 });

    // Verify it's NOT inside the ag-Grid container
    // The button should be in the CardHeader (mp-add-btn), not in grid cells
    const gridContainer = page.locator('.ag-root').first();
    const isInGrid = await addButton.evaluate((btn) => {
      const grid = document.querySelector('.ag-root');
      return grid?.contains(btn) ?? false;
    });

    expect(isInGrid).toBe(false);

    // Verify it's in a reasonable location in the header area
    const boundingBox = await addButton.boundingBox();
    expect(boundingBox).toBeTruthy();
    expect(boundingBox.y).toBeLessThan(200); // Rough heuristic: button is near top
  });
});
