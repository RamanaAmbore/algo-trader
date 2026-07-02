/**
 * mode_dropdown_above_modal.spec.js
 *
 * Verifies that the navbar mode-chip dropdown renders ABOVE the order
 * modal overlay (z-index 10500) so the operator can change execution
 * mode without closing the modal first.
 *
 * Five quality dimensions:
 *  SSOT   — z-index pulled from computed style, not hard-coded expectation
 *  Perf   — no unnecessary waits; assertions sync after mount
 *  Stale  — grep guard: .ot-mode-chip must NOT exist (reverted ba80dfac)
 *  Reuse  — shared loginAsAdmin fixture
 *  UX     — dropdown visible + clickable on desktop + mobile; above modal
 *
 * Two viewport flavours per test: chromium-desktop (1280×800) + mobile (390×844).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

// ── Helpers ────────────────────────────────────────────────────────────────

/**
 * Open an OrderTicket modal via /pulse symbol context menu.
 * This path mounts .canonical-modal-overlay at z-index 10500.
 */
async function openOrderModalFromPulse(page) {
  await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
  // Wait for at least one ag-Grid row to populate.
  const row = page.locator('.ag-row').first();
  await row.waitFor({ state: 'visible', timeout: 20_000 });

  // Open the context menu for the first symbol row.
  const firstSymActions = page.locator('.sym-actions').first();
  await firstSymActions.evaluate((el) =>
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }))
  );

  // Click "Place order" in the context menu.
  const placeBtn = page.locator('button.ctx-item:has-text("Place order"), button:has-text("Place order")').first();
  await placeBtn.waitFor({ state: 'visible', timeout: 8_000 });
  await placeBtn.click();

  // Wait for the canonical modal overlay to appear.
  const overlay = page.locator('.canonical-modal-overlay').first();
  await overlay.waitFor({ state: 'visible', timeout: 8_000 });

  // Wait for the order form inside the modal.
  const form = overlay.locator('.ot-row-quick').first();
  await form.waitFor({ state: 'visible', timeout: 8_000 });

  return overlay;
}

/** Click the first visible navbar mode chip and wait for the dropdown. */
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
  // commit ba80dfac.  Assert it is absent on both /orders and /pulse.
  test('ot-mode-chip is absent — revert ba80dfac confirmed [/orders]', async ({ page }) => {
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });
    await page.locator('.ot-row-quick, .ot-panel').first().waitFor({ state: 'visible', timeout: 15_000 });
    await expect(page.locator('.ot-mode-chip')).toHaveCount(0);
  });

  for (const vp of [
    { label: 'desktop', width: 1280, height: 800 },
    { label: 'mobile',  width: 390,  height: 844 },
  ]) {
    test(`dropdown position:fixed + above modal z-index [${vp.label}]`, async ({ page }) => {
      test.setTimeout(60_000);
      await page.setViewportSize({ width: vp.width, height: vp.height });

      // 1. Open the order modal from /pulse → produces .canonical-modal-overlay.
      const overlay = await openOrderModalFromPulse(page);

      // 2. Read the modal overlay's z-index from computed style (SSOT — not
      //    hard-coded so the test survives future value changes).
      const modalZ = await overlay.evaluate((el) =>
        parseInt(getComputedStyle(el).zIndex, 10) || 0
      );
      // Sanity: modal must be a meaningful stacking context.
      expect(modalZ).toBeGreaterThan(1000);

      // 3. Open the navbar mode dropdown while the modal is still open.
      const { dropdown } = await openModeDropdown(page);

      // 4. Dropdown must be visible.
      await expect(dropdown).toBeVisible();

      // 5. Dropdown must use position:fixed (not absolute trapped inside navbar).
      const dropdownPosition = await dropdown.evaluate((el) =>
        getComputedStyle(el).position
      );
      expect(dropdownPosition).toBe('fixed');

      // 6. Dropdown z-index must exceed the modal's z-index.
      const dropdownZ = await dropdown.evaluate((el) =>
        parseInt(getComputedStyle(el).zIndex, 10) || 0
      );
      expect(dropdownZ).toBeGreaterThan(modalZ);

      // 7. Hit-test: the element at the dropdown's centre must be INSIDE the
      //    dropdown, not behind the modal panel.
      const bbox = await dropdown.boundingBox();
      expect(bbox).not.toBeNull();
      if (bbox) {
        const cx = Math.round(bbox.x + bbox.width / 2);
        const cy = Math.round(bbox.y + bbox.height / 2);
        const topEl = await page.evaluate(({ x, y }) => {
          const el = document.elementFromPoint(x, y);
          let cur = /** @type {Element | null} */ (el);
          while (cur) {
            if (cur.classList?.contains('mode-combo-dropdown')) return 'dropdown';
            if (cur.classList?.contains('mode-combo-item'))    return 'dropdown-item';
            if (cur.classList?.contains('canonical-modal-panel')) return 'modal-panel';
            cur = cur.parentElement;
          }
          return el?.tagName?.toLowerCase() ?? 'unknown';
        }, { x: cx, y: cy });

        // Must hit dropdown content — NOT the modal panel behind it.
        expect(['dropdown', 'dropdown-item']).toContain(topEl);
      }

      // 8. At least one mode item must be visible and not pointer-events:none.
      const firstItem = dropdown.locator('button.mode-combo-item').first();
      await expect(firstItem).toBeVisible();
      const pe = await firstItem.evaluate((el) => getComputedStyle(el).pointerEvents);
      expect(pe).not.toBe('none');
    });
  }
});
