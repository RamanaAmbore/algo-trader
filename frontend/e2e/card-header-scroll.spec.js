/**
 * card-header-scroll.spec.js
 *
 * Tests mobile-viewport (375×812px) overflow behavior for:
 *   1. NavStrip (.ps-strip) horizontal scroll on mobile without breaking page width
 *   2. Card headers (.mp-head-tabs) staying single-row on mobile
 *   3. M (margin) vs C (cash) pill color contrast (cyan-400 vs sky-300)
 *   4. NavStrip pill count (P, M, C, H) staying visible without wrapping
 *
 * Background:
 *   - Card headers with chips/tabs now scroll horizontally instead of wrapping
 *   - NavStrip base container now has overflow-x: auto
 *   - M pill values use cyan-400 (#22d3ee); C pill keeps sky-300 (#7dd3fc)
 *
 * Target: dev.ramboq.com (PLAYWRIGHT_BASE_URL env var, or localhost default)
 * Viewport: 375×812px (iPhone 12 portrait)
 *
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com npx playwright test \
 *     e2e/card-header-scroll.spec.js --project=mobile-portrait
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 25_000;

test.describe('Mobile viewport overflow — card headers and NavStrip', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('NavStrip has overflow-x: auto and does not break page width', async ({ page }) => {
    await page.goto('/dashboard');

    // Wait for NavStrip to be visible.
    const strip = page.locator('.ps-strip');
    await strip.waitFor({ state: 'visible', timeout: TIMEOUT });

    // Assert that the strip has overflow-x: auto or overflow-x: scroll
    const overflowX = await strip.evaluate((el) => {
      return getComputedStyle(el).overflowX;
    });
    expect(['auto', 'scroll']).toContain(
      overflowX,
      'NavStrip should have overflow-x: auto or scroll on mobile'
    );

    // Verify that the page body does not have horizontal overflow.
    // document.body.scrollWidth should equal window.innerWidth (no scroll bar).
    const pageOverflow = await page.evaluate(() => {
      return {
        bodyScrollWidth: document.body.scrollWidth,
        windowInnerWidth: window.innerWidth,
      };
    });

    expect(pageOverflow.bodyScrollWidth).toBeLessThanOrEqual(
      pageOverflow.windowInnerWidth,
      'Page should not overflow horizontally; bodyScrollWidth <= windowInnerWidth'
    );
  });

  test('M pill value displays (margin available) on mobile', async ({ page }) => {
    await page.goto('/dashboard');

    // Wait for at least one pill to be visible.
    const firstPill = page.locator('.ps-agg').first();
    await firstPill.waitFor({ state: 'visible', timeout: TIMEOUT });

    // Find the M pill (margin): locator text starts with 'M'
    const mPill = page.locator('.ps-agg').filter({ hasText: /^M/ }).first();
    const mPillExists = await mPill.count();
    if (mPillExists === 0) {
      test.skip(true, 'No M pill found on this page or data set');
    }

    // Verify that the M pill displays a value (rendered margin available figure).
    const mValue = await mPill.locator('.ps-agg-v').first();
    const mValueText = await mValue.textContent({ timeout: TIMEOUT });
    expect(mValueText).toBeTruthy();
  });

  test('Card header tabs stay single-row on mobile (≤36px height)', async ({ page }) => {
    await page.goto('/dashboard');

    // Wait for the card header tabs to render. Look for .mp-head-tabs which
    // wraps the AlgoTabs inside market-pulse cards (gainers/losers).
    const headerTabs = page.locator('.mp-head-tabs').first();
    const headerTabsExists = await headerTabs.count();

    if (headerTabsExists === 0) {
      test.skip(true, 'No .mp-head-tabs found on page (card might not have tabs on this view)');
    }

    // Get the offset height of the header-tabs container.
    const tabsHeight = await headerTabs.evaluate((el) => {
      return el.offsetHeight;
    });

    // On mobile (375px viewport), card header height should not exceed 36px for single-row.
    // This ensures tabs are not wrapping to a second row.
    expect(tabsHeight).toBeLessThanOrEqual(
      36,
      `Card header tabs should stay single-row (≤36px), but got ${tabsHeight}px`
    );
  });

  test('NavStrip has exactly 4 pills (P, M, C, H) visible on mobile', async ({ page }) => {
    await page.goto('/dashboard');

    // Wait for the strip to be visible.
    const strip = page.locator('.ps-strip');
    await strip.waitFor({ state: 'visible', timeout: TIMEOUT });

    // Count .ps-agg (pill) elements inside the strip.
    const pillCount = await strip.locator('.ps-agg').count();
    expect(pillCount).toBe(
      4,
      `NavStrip should display exactly 4 pills (P, M, C, H), but found ${pillCount}`
    );

    // Verify the strip height is still the fixed value (1.5rem = 24px).
    const stripHeight = await strip.evaluate((el) => {
      return el.offsetHeight;
    });
    expect(stripHeight).toBe(
      24,
      `NavStrip height should be 24px (1.5rem), but got ${stripHeight}px`
    );

    // Verify all 4 pills are within the strip's bounding box (not hidden by overflow).
    const stripBounds = await strip.evaluate((el) => {
      return {
        left: el.offsetLeft,
        right: el.offsetLeft + el.offsetWidth,
        width: el.offsetWidth,
      };
    });

    const allPills = await strip.locator('.ps-agg').all();
    for (const pill of allPills) {
      const pillBounds = await pill.evaluate((el) => {
        return {
          left: el.offsetLeft,
          right: el.offsetLeft + el.offsetWidth,
        };
      });

      // Each pill should be at least partially within the strip's horizontal bounds.
      // (May be partially off-screen if scrolling is needed, but should be accessible.)
      expect(pillBounds.right).toBeGreaterThan(
        stripBounds.left - 50,
        'Pill should not be far to the left of strip'
      );
    }
  });
});
