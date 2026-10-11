/**
 * console_guide_page.spec.js
 *
 * New in-app operator guide for the Console CLI grammar:
 *   frontend/src/routes/(algo)/console/guide/+page.svelte
 *
 * Covers:
 *   1. The guide page renders at /console/guide and shows the expected
 *      section headers (Orders, Agents, Event agents, Brackets, Tokens,
 *      Meta tags, Quick reference).
 *   2. Clicking the "Guide" link in /console's page-header navigates to
 *      /console/guide successfully.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;

test.describe('Console CLI guide page', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('renders with all expected section headers', async ({ page }) => {
    await page.goto('/console/guide');
    await expect(page.locator('h1.page-title-chip', { hasText: 'Console CLI Guide' }))
      .toBeVisible({ timeout: TIMEOUT });

    const expectedHeadings = [
      /Orders/,
      /^Agents/,
      /Event agents/,
      /Brackets/,
      /^Tokens/,
      /Meta tags/,
      /Quick reference/,
    ];
    for (const re of expectedHeadings) {
      await expect(page.locator('h2.section-heading', { hasText: re }).first())
        .toBeVisible({ timeout: TIMEOUT });
    }

    // Spot-check one real, confirmed-working example statement renders
    // verbatim (regression guard against accidental rewording that
    // would desync the guide from the actual grammar).
    await expect(page.locator('pre.cg-code', {
      hasText: 'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)',
    }).first()).toBeVisible();
  });

  test('Guide link on /console navigates to /console/guide', async ({ page }) => {
    await page.goto('/console');
    const guideLink = page.locator('a.console-guide-link');
    await expect(guideLink).toBeVisible({ timeout: TIMEOUT });
    await guideLink.click();
    await expect(page).toHaveURL(/\/console\/guide$/, { timeout: TIMEOUT });
    await expect(page.locator('h1.page-title-chip', { hasText: 'Console CLI Guide' }))
      .toBeVisible({ timeout: TIMEOUT });
  });
});
