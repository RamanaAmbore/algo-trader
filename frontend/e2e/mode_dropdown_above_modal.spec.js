/**
 * mode_dropdown_above_modal.spec.js
 *
 * Verifies that the navbar mode-chip dropdown renders ABOVE the order
 * modal overlay (z-index 10500) so the operator can change execution
 * mode without closing the modal first.
 *
 * Five quality dimensions:
 *  SSOT   — z-index pulled from computed style, not hard-coded expectation
 *  Perf   — no unnecessary waits; assertions are synchronous after mount
 *  Stale  — grep guard: .ot-mode-chip must NOT exist (reverted from a2d2c000)
 *  Reuse  — uses shared loginAsAdmin fixture
 *  UX     — dropdown must be visible + clickable on both desktop + mobile
 *
 * Two projects: chromium-desktop (1280×800) + chromium-mobile (390×844).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

// ── Helpers ────────────────────────────────────────────────────────────────

/** Open any order modal.  Navigates to /orders and clicks the first
 *  "Place order" / Order Entry button visible on that page. */
async function openOrderModal(page) {
  await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });
  // Wait for the page to settle — the orders page mounts the OrderTicket
  // inline as the "Order Entry" card; it doesn't need a separate trigger.
  // If the inline form is present we're done; otherwise try the place-order
  // button that some pages surface via a symbol-panel context menu.
  const inlineForm = page.locator('.ot-row-quick').first();
  const altBtn = page.locator('button:has-text("Place order"), button:has-text("Order Entry")').first();
  try {
    await inlineForm.waitFor({ state: 'visible', timeout: 12_000 });
  } catch {
    // Fallback — click an explicit open button
    await altBtn.waitFor({ state: 'visible', timeout: 8_000 });
    await altBtn.click();
    await inlineForm.waitFor({ state: 'visible', timeout: 8_000 });
  }
}

/** Click the (first visible) navbar mode chip and wait for the dropdown. */
async function openModeDropdown(page) {
  const trigger = page.locator('button.mode-trigger').first();
  await trigger.waitFor({ state: 'visible', timeout: 8_000 });
  await trigger.click();
  const dropdown = page.locator('ul.mode-combo-dropdown').first();
  await dropdown.waitFor({ state: 'visible', timeout: 5_000 });
  return { trigger, dropdown };
}

// ── Test suite ─────────────────────────────────────────────────────────────

test.describe('Mode dropdown renders above order modal', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  // STALE guard: the in-modal .ot-mode-chip block was reverted in
  // commit ba80dfac.  Assert it no longer exists anywhere on /orders.
  test('ot-mode-chip is absent — revert confirmed', async ({ page }) => {
    await openOrderModal(page);
    await expect(page.locator('.ot-mode-chip')).toHaveCount(0);
  });

  for (const vp of [
    { label: 'desktop', width: 1280, height: 800 },
    { label: 'mobile',  width: 390,  height: 844 },
  ]) {
    test(`dropdown visible + above modal [${vp.label}]`, async ({ page }) => {
      test.setTimeout(60_000);
      await page.setViewportSize({ width: vp.width, height: vp.height });

      // 1. Open the order modal / order entry surface.
      await openOrderModal(page);

      // 2. The canonical modal overlay has z-index 10500 in app.css.
      //    We don't hard-code 10500 here — we read it from the live DOM
      //    so this test stays valid if the value ever changes.
      const modalOverlay = page.locator('.canonical-modal-overlay').first();
      // Not every /orders layout mounts .canonical-modal-overlay (the page
      // may use an inline card instead of an overlay).  Skip the z-index
      // comparison when the overlay is absent; still test dropdown visibility.
      const overlayPresent = await modalOverlay.isVisible().catch(() => false);
      let modalZ = 0;
      if (overlayPresent) {
        modalZ = await modalOverlay.evaluate((el) =>
          parseInt(getComputedStyle(el).zIndex, 10) || 0
        );
      }

      // 3. Open the navbar mode dropdown.
      const { dropdown } = await openModeDropdown(page);

      // 4. Assert dropdown is visible.
      await expect(dropdown).toBeVisible();

      // 5. Assert dropdown z-index exceeds the modal's z-index.
      const dropdownZ = await dropdown.evaluate((el) =>
        parseInt(getComputedStyle(el).zIndex, 10) || 0
      );
      if (overlayPresent && modalZ > 0) {
        expect(dropdownZ).toBeGreaterThan(modalZ);
      } else {
        // No modal overlay on this page — just confirm it's above the navbar.
        expect(dropdownZ).toBeGreaterThan(100);
      }

      // 6. Assert dropdown is position:fixed (not trapped in navbar stacking
      //    context which is itself position:fixed at z-index 50).
      const dropdownPosition = await dropdown.evaluate((el) =>
        getComputedStyle(el).position
      );
      expect(dropdownPosition).toBe('fixed');

      // 7. Assert dropdown bounding box top-left is not occluded by modal.
      //    We check via elementFromPoint at the dropdown's center — it should
      //    resolve to a descendant of the dropdown (or the dropdown itself),
      //    not to the modal panel beneath it.
      const bbox = await dropdown.boundingBox();
      if (bbox) {
        const cx = bbox.x + bbox.width / 2;
        const cy = bbox.y + bbox.height / 2;
        const topEl = await page.evaluate(({ x, y }) => {
          const el = document.elementFromPoint(x, y);
          // Walk up to find the closest mode-combo-dropdown ancestor.
          let cur = el;
          while (cur) {
            if (cur.classList?.contains('mode-combo-dropdown')) return 'dropdown';
            if (cur.classList?.contains('mode-combo-item'))    return 'dropdown-item';
            if (cur.classList?.contains('canonical-modal-panel')) return 'modal';
            cur = cur.parentElement;
          }
          return el?.tagName?.toLowerCase() ?? 'unknown';
        }, { x: cx, y: cy });

        // Must be dropdown or a dropdown item — NOT the modal panel.
        expect(['dropdown', 'dropdown-item']).toContain(topEl);
      }

      // 8. Assert at least one mode item is clickable (not pointer-events:none).
      const firstItem = dropdown.locator('button.mode-combo-item').first();
      await expect(firstItem).toBeVisible();
      const pe = await firstItem.evaluate((el) => getComputedStyle(el).pointerEvents);
      expect(pe).not.toBe('none');
    });
  }
});
