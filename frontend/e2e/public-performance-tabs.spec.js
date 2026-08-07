/**
 * Public /performance page — tab switching and alignment
 *
 * Tests two bug fixes:
 * 1. Clicking Positions/Holdings tabs now switches visible sections (was broken)
 * 2. Both tab strips are left-aligned with each other (was misaligned)
 */

import { test, expect } from '@playwright/test';

test.describe('Public /performance page — tab switching and alignment', () => {
  test('Positions tab is active by default', async ({ page }) => {
    await page.goto('/performance');

    // Wait for the tabs row to be visible and grids to attach
    const tabsRow = page.locator('.tabs-row');
    await tabsRow.waitFor({ state: 'visible', timeout: 10_000 });

    // Wait for at least one ag-grid to render (indicates content is loaded)
    await page.locator('.ag-theme-quartz').first().waitFor({ state: 'attached', timeout: 15_000 });

    // Assert Positions Summary section (index 0) is visible by default
    const allSections = page.locator('section');
    const positionsSummary = allSections.nth(0);
    await expect(positionsSummary).not.toHaveClass(/hidden/);

    // Assert Holdings Summary section (index 1) is hidden
    const holdingsSummary = allSections.nth(1);
    await expect(holdingsSummary).toHaveClass(/hidden/);
  });

  test('Clicking Holdings tab shows Holdings and hides Positions', async ({ page }) => {
    await page.goto('/performance');

    // Wait for tabs row and content
    const tabsRow = page.locator('.tabs-row');
    await tabsRow.waitFor({ state: 'visible', timeout: 10_000 });
    await page.locator('.ag-theme-quartz').first().waitFor({ state: 'attached', timeout: 15_000 });

    // Click the Holdings tab — use role="tab" selector
    const holdingsTab = tabsRow.locator('button[role="tab"]').nth(1);
    await holdingsTab.click();

    // Get all sections and wait for the second Summary section (Holdings) to not be hidden
    const allSections = page.locator('section');
    const holdingsSummary = allSections.nth(1);
    await expect(holdingsSummary).not.toHaveClass(/hidden/);

    // Assert Positions Summary section (index 0) is now hidden
    const positionsSummary = allSections.nth(0);
    await expect(positionsSummary).toHaveClass(/hidden/);
  });

  test('Clicking Positions tab after Holdings shows Positions and hides Holdings', async ({ page }) => {
    await page.goto('/performance');

    // Wait for tabs row and content
    const tabsRow = page.locator('.tabs-row');
    await tabsRow.waitFor({ state: 'visible', timeout: 10_000 });
    await page.locator('.ag-theme-quartz').first().waitFor({ state: 'attached', timeout: 15_000 });

    const allSections = page.locator('section');
    const positionsSummary = allSections.nth(0);
    const holdingsSummary = allSections.nth(1);

    // Click Holdings tab first
    const holdingsTab = tabsRow.locator('button[role="tab"]').nth(1);
    await holdingsTab.click();

    // Verify Holdings is now visible
    await expect(holdingsSummary).not.toHaveClass(/hidden/);

    // Now click Positions tab to switch back
    const positionsTab = tabsRow.locator('button[role="tab"]').nth(0);
    await positionsTab.click();

    // Assert Positions section is now visible
    await expect(positionsSummary).not.toHaveClass(/hidden/);

    // Assert Holdings section is hidden
    await expect(holdingsSummary).toHaveClass(/hidden/);
  });

  test('Both tab strips share the same left offset (alignment)', async ({ page }) => {
    await page.goto('/performance');

    // Wait for both tab strips
    const fundsNavTabs = page.locator('.funds-nav-tabs');
    const tabsRow = page.locator('.tabs-row');

    await fundsNavTabs.waitFor({ state: 'visible', timeout: 10_000 });
    await tabsRow.waitFor({ state: 'visible', timeout: 10_000 });

    // Get first tab button in each strip (NAV tab and Positions tab)
    // Use role="tab" selector for semantic anchoring
    const navTabButton = fundsNavTabs.locator('button[role="tab"]').first();
    const positionsTabButton = tabsRow.locator('button[role="tab"]').first();

    // Wait for buttons to be in the DOM and get their bounding boxes
    await navTabButton.waitFor({ state: 'attached', timeout: 5_000 });
    await positionsTabButton.waitFor({ state: 'attached', timeout: 5_000 });

    const navBox = await navTabButton.boundingBox();
    const posBox = await positionsTabButton.boundingBox();

    expect(navBox).not.toBeNull();
    expect(posBox).not.toBeNull();

    // Assert their left X coordinates are within 4px of each other
    const navTabLeft = navBox.x;
    const posTabLeft = posBox.x;

    expect(Math.abs(navTabLeft - posTabLeft)).toBeLessThanOrEqual(4);
  });
});
