// Regression guard: OrderDepth must not render a second LTP chip above
// the bid/ask ladder. The canonical LTP lives in the SymbolPanel tab bar
// (.oes-tab-ltp). The depth panel header (.ot-depth) must never contain
// an element with the class .ot-depth-ltp, and must not render any
// visible text containing "LTP" inside the depth container.
//
// Covers desktop (1280×800) and mobile (390×844) viewports.
// Each viewport runs as a named test.use block so the spec is
// self-contained without relying on the config project matrix.
//
// Auth: uses loginAsAdmin() fast-path (globalSetup-cached token).
// No live broker needed — the depth section is structurally absent or
// shows em-dashes off-hours; either way .ot-depth-ltp must be absent.

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

/** Navigate to /orders and wait for SymbolPanel to mount. */
async function gotoOrders(page) {
  await loginAsAdmin(page);
  await page.goto('/orders');
  // Wait for the AlgoTabs tab strip that SymbolPanel renders.
  await expect(page.locator('.algo-tabs-strip').first()).toBeVisible({ timeout: 20_000 });
}

/**
 * Core assertions, viewport-agnostic.
 * @param {import('@playwright/test').Page} page
 */
async function assertNoDepthLtp(page) {
  // 1. The removed CSS class must not exist anywhere in the DOM.
  await expect(page.locator('.ot-depth-ltp')).toHaveCount(0);

  // 2. Inside the depth panel container, no element's textContent
  //    should start with or equal "LTP" (the removed chip label).
  //    "OI", "Vol", "Spd", "Prev" are allowed; bare "LTP" is not.
  //    We search within .ot-depth if it exists; skip if absent (off-hours).
  const depthContainer = page.locator('.ot-depth').first();
  if (await depthContainer.isVisible({ timeout: 3_000 }).catch(() => false)) {
    // Collect all leaf text nodes inside the depth panel.
    const containsLtpLabel = await depthContainer.evaluate((el) => {
      // Walk all elements; return true if any has textContent that
      // is exactly "LTP" or starts with "LTP " (the removed chip pattern).
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_ELEMENT);
      let node = walker.nextNode();
      while (node) {
        const text = /** @type {Element} */ (node).textContent?.trim() ?? '';
        // Match "LTP" or "LTP ₹..." — but not "OI", "Prev", "Spd", etc.
        if (/^LTP(\s|₹|$)/.test(text)) return true;
        node = walker.nextNode();
      }
      return false;
    });
    expect(containsLtpLabel, 'depth panel must not contain an LTP label').toBe(false);
  }

  // 3. If the tab bar LTP chip is rendered (live data, market open),
  //    confirm it carries the expected class and text — i.e. the
  //    canonical source is still intact.
  const tabLtp = page.locator('.oes-tab-ltp').first();
  if (await tabLtp.isVisible({ timeout: 2_000 }).catch(() => false)) {
    const label = await tabLtp.locator('.oes-tab-ltp-label').first().textContent();
    expect(label?.trim()).toBe('LTP');
  }
}

test.describe('OrderDepth — LTP dedup guard (desktop 1280×800)', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test('depth panel does not show a second LTP chip on /orders', async ({ page }) => {
    await gotoOrders(page);
    await assertNoDepthLtp(page);
  });
});

test.describe('OrderDepth — LTP dedup guard (mobile 390×844)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('depth panel does not show a second LTP chip on /orders', async ({ page }) => {
    await gotoOrders(page);
    await assertNoDepthLtp(page);
  });
});
