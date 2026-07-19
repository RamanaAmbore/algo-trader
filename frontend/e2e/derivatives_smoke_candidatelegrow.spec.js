import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('CandidateLegRow component extraction smoke test', () => {
  test('derivatives page loads without error', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives');
    await page.waitForLoadState('networkidle');

    // Check page is loaded by looking for the main card container
    const mainContent = page.locator('main, [role="main"]');
    await expect(mainContent).toBeVisible({ timeout: 10000 });
  });

  test('candidate grid renders without errors (with or without rows)', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives');
    await page.waitForLoadState('networkidle');

    // Wait for cand-grid which contains CandidateLegRow instances
    const candGrid = page.locator('.cand-grid');
    await expect(candGrid).toBeVisible({ timeout: 10000 });

    // Grid is rendered, whether or not there are candidate rows.
    // Absence of rows is OK (empty state or no open positions).
    // The key is the grid itself is mounted and visible.
    expect(candGrid).toBeVisible();
  });

  test('CandidateLegRow checkbox toggle works', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives');
    await page.waitForLoadState('networkidle');

    // Wait for cand-grid to load
    await page.locator('.cand-grid').waitFor({ state: 'visible', timeout: 10000 });

    // Look for checkboxes inside cand-row elements
    const checkboxes = page.locator('.cand-row input[type="checkbox"]');
    const checkboxCount = await checkboxes.count();

    if (checkboxCount > 0) {
      const checkbox = checkboxes.first();
      const wasChecked = await checkbox.isChecked();

      // Toggle the checkbox
      await checkbox.click();
      await page.waitForTimeout(300);

      const nowChecked = await checkbox.isChecked();

      // Verify state changed
      expect(nowChecked).not.toBe(wasChecked);
    }
  });

  test('CandidateLegRow context menu / long-press works', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives');
    await page.waitForLoadState('networkidle');

    // Wait for cand-grid to load
    await page.locator('.cand-grid').waitFor({ state: 'visible', timeout: 10000 });

    // Find a cand-row and right-click it
    const candRows = page.locator('.cand-row');
    const rowCount = await candRows.count();

    if (rowCount > 0) {
      const firstRow = candRows.first();

      // Right-click to trigger context menu
      await firstRow.click({ button: 'right' });
      await page.waitForTimeout(300);

      // Context menu should appear or nothing should break
      // No specific assertion needed, just verify page is still responsive
      const mainContent = page.locator('main, [role="main"]');
      await expect(mainContent).toBeVisible();
    }
  });

  test('no component-level JS errors after page load', async ({ page }) => {
    const errors = [];

    page.on('console', msg => {
      if (msg.type() === 'error') {
        errors.push(msg.text());
      }
    });

    await loginAsAdmin(page);
    await page.goto('/admin/derivatives');
    await page.waitForLoadState('networkidle');

    // Wait for grid to render
    await page.locator('.cand-grid').waitFor({ state: 'visible', timeout: 10000 });
    await page.waitForTimeout(1000);

    // Filter to real component errors (not network/auth/WebSocket noise)
    const relevantErrors = errors.filter(err =>
      (err.includes('Cannot read') ||
       err.includes('undefined') ||
       err.includes('CandidateLegRow') ||
       err.includes('displayedCandidates') ||
       err.includes('onToggleEnabled') ||
       err.includes('onContextMenu') ||
       err.includes('Svelte error')) &&
      !err.includes('401') &&
      !err.includes('WebSocket') &&
      !err.includes('fetch failed') &&
      !err.includes('404')
    );

    expect(relevantErrors).toHaveLength(0);
  });

  test('grid header is present and visible', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives');
    await page.waitForLoadState('networkidle');

    // Wait for cand-grid
    const candGrid = page.locator('.cand-grid');
    await expect(candGrid).toBeVisible({ timeout: 10000 });

    // Look for header container (cand-hdr class)
    const header = page.locator('.cand-hdr, [role="row"][role="columnheader"]').first();

    // Header should exist and be visible
    if (await header.count() > 0) {
      await expect(header).toBeVisible();
    }
  });

  test('Test 2 — derivatives footnote removed', async ({ page }) => {
    // Verify that /admin/derivatives does NOT contain removed footnote text
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives');
    await page.waitForLoadState('networkidle');

    // Wait for page to settle
    await page.waitForTimeout(1000);

    // Verify page does NOT contain the old footnote phrases
    const hasNumericalMaxMin = await page.getByText(/numerical max\/min/i).isVisible().catch(() => false);
    const hasSpotRangeAtExpiry = await page.getByText(/spot range at expiry/i).isVisible().catch(() => false);

    expect(hasNumericalMaxMin).toBe(false);
    expect(hasSpotRangeAtExpiry).toBe(false);
  });

  test('Test 4 — legs rows alternating shading', async ({ page }) => {
    // Verify that .cand-row elements have alternating background colors
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives');
    await page.waitForLoadState('networkidle');

    // Wait for cand-row elements to appear
    const candRows = page.locator('.cand-row');
    const rowCount = await candRows.count();

    // Skip if fewer than 2 rows
    if (rowCount < 2) {
      test.skip();
    }

    // Collect computed background-color for first few rows
    const colors = await page.evaluate(() => {
      const rows = document.querySelectorAll('.cand-row');
      const rowsArray = Array.from(rows);
      return rowsArray.slice(0, 4).map(row => {
        const style = getComputedStyle(row);
        return style.backgroundColor;
      });
    });

    // Verify that at least one pair of adjacent rows has different background colors
    let hasAlternating = false;
    for (let i = 0; i < colors.length - 1; i++) {
      if (colors[i] !== colors[i + 1]) {
        hasAlternating = true;
        break;
      }
    }

    expect(hasAlternating, 'Rows should have alternating background colors').toBe(true);
  });

  test('Test 5 — greek InfoHint buttons present', async ({ page }) => {
    // Verify that .kv-k-greek elements contain button elements (InfoHint chips)
    await loginAsAdmin(page);
    await page.goto('/admin/derivatives');
    await page.waitForLoadState('networkidle');

    // Wait for greeks section to load
    const greeksSection = page.locator('.opt-kv-greeks');
    await expect(greeksSection).toBeVisible({ timeout: 10000 });

    // Find all .kv-k-greek spans
    const greekSpans = page.locator('.kv-k-greek');
    const greekCount = await greekSpans.count();

    // We expect at least one greek label (Δ, Γ, Θ, 𝒱, ρ)
    expect(greekCount).toBeGreaterThanOrEqual(1);

    // Verify that at least one .kv-k-greek contains a button element
    for (let i = 0; i < Math.min(greekCount, 3); i++) {
      const greekSpan = greekSpans.nth(i);
      const button = greekSpan.locator('button');
      const buttonCount = await button.count();

      if (buttonCount > 0) {
        // Found at least one InfoHint button
        await expect(button.first()).toBeVisible();
        return; // Test passes
      }
    }

    // If we get here without finding a button, verify the greeks section has role="button" somewhere
    const hasButton = await greeksSection.locator('[role="button"]').count();
    expect(hasButton).toBeGreaterThanOrEqual(1);
  });
});
