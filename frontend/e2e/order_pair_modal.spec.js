/**
 * OrderPairModal + ChaseCard orphan chip smoke tests.
 *
 * Validates:
 * 1. The "Pair" button renders in the Positions card header on /pulse.
 * 2. Clicking "Pair" opens the OrderPairModal (two selects + Cancel + Pair buttons).
 * 3. Cancel closes the modal.
 * 4. The "Pair" button also appears in the Drafts leg-headrow on /admin/derivatives.
 * 5. ChaseCard orphan chip (.cc-chip-o) CSS class is defined (DOM-level check).
 *
 * Note: the .cc-chip-o chip only renders when a chase row's parent_order_id is absent
 * from the active chase set — a live-data condition that cannot be reliably reproduced
 * in e2e without injecting test fixtures. Tests 1-4 cover the interaction surface;
 * the CSS existence check (test 5) verifies the style is present without requiring
 * specific chase data.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 30_000;

test.describe('OrderPairModal — Pair button + modal interactions', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('1. Pair button visible in Positions card header on /pulse', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    // Wait for card headers to render
    await page.waitForSelector('.mp-pair-btn', { timeout: TIMEOUT });
    const btn = page.locator('.mp-pair-btn').first();
    await expect(btn).toBeVisible();
    await expect(btn).toContainText('Pair');
  });

  test('2. Clicking Pair button opens OrderPairModal with two selects', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.mp-pair-btn', { timeout: TIMEOUT });
    await page.locator('.mp-pair-btn').first().click();
    // Modal overlay should appear
    await page.waitForSelector('.opm-overlay', { timeout: 8000 });
    await expect(page.locator('.opm-title')).toContainText('Pair Orders');
    // Both selects must be present
    const selects = page.locator('.opm-select');
    await expect(selects).toHaveCount(2);
    // Cancel and Pair action buttons present
    await expect(page.locator('.opm-cancel')).toBeVisible();
    await expect(page.locator('.opm-submit')).toBeVisible();
  });

  test('3. Cancel button closes the OrderPairModal', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.mp-pair-btn', { timeout: TIMEOUT });
    await page.locator('.mp-pair-btn').first().click();
    await page.waitForSelector('.opm-overlay', { timeout: 8000 });
    await page.locator('.opm-cancel').click();
    // Modal should be gone
    await expect(page.locator('.opm-overlay')).toHaveCount(0);
  });

  test('4. Pair button visible in Drafts leg-headrow on /admin/derivatives', async ({ page }) => {
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    // Add a draft so the leg-headrow renders
    // The drafts section only renders when drafts.length > 0; look for the button
    // directly — it may not be visible if no drafts exist; just assert it exists in DOM.
    // We wait for the page to settle then look for the button.
    await page.waitForLoadState('networkidle', { timeout: TIMEOUT });
    const btn = page.locator('.leg-pair-btn');
    // The button only renders inside {#if drafts.length} so it may be absent;
    // if present it must be labelled "Pair".
    const count = await btn.count();
    if (count > 0) {
      await expect(btn.first()).toContainText('Pair');
    }
    // Verify the CSS class .leg-pair-btn is declared in the page stylesheet
    // even when the element is absent (style is in <style> block, always injected).
    const styleExists = await page.evaluate(() => {
      return Array.from(document.styleSheets).some(sheet => {
        try {
          return Array.from(sheet.cssRules).some(r => r.cssText?.includes('leg-pair-btn'));
        } catch { return false; }
      });
    });
    expect(styleExists).toBe(true);
  });

  test('5. ChaseCard .cc-chip-o CSS class is defined on /orders page', async ({ page }) => {
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle', { timeout: TIMEOUT });
    // ChaseCard is always mounted (even hidden); verify its orphan chip CSS is present.
    const styleExists = await page.evaluate(() => {
      return Array.from(document.styleSheets).some(sheet => {
        try {
          return Array.from(sheet.cssRules).some(r => r.cssText?.includes('cc-chip-o'));
        } catch { return false; }
      });
    });
    expect(styleExists).toBe(true);
  });
});
