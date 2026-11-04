/**
 * order_modal_label_no_left_bar.spec.js
 *
 * 2026-11-03, operator: "in orders modal, the left vertical bar to
 * order label should not be present."
 *
 * `.oes-modal-name` (the "ORDERS" label in the order-ticket modal's
 * header) carried the shared `.algo-label-decor` class (app.css),
 * which adds a `border-left: 3px solid currentColor` bar + matching
 * left padding — the same decoration `.ch-title`/`.mp-bucket-label`
 * use elsewhere, explicitly flagged in this file's own 2026-11-03
 * canonical-header-padding comment as "flagged for a future pass if
 * the inset becomes visually objectionable" (it grew more visible
 * once this header's own padding grew to match ChartModal/
 * NavBreakdown's canonical inset).
 *
 * Fix: a scoped `.oes-modal-name.algo-label-decor` override removes
 * the border/padding/radius, while deliberately KEEPING the shared
 * class's `display: inline-flex; align-items: center; align-self:
 * stretch` (the icon+text layout and full-header-height stretch
 * still need those) — the shared `.algo-label-decor` class itself is
 * untouched, scoped to this one label only.
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('Order ticket modal — "ORDERS" label has no left border bar', () => {
  test('computed border-left/padding-left are both zero on .oes-modal-name', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: 30_000 });

    const orderBtn = page.locator('.pha-order:visible').first();
    await expect(orderBtn).toBeVisible({ timeout: 15_000 });
    await orderBtn.click();

    const label = page.locator('.oes-modal-name');
    await expect(label).toBeVisible({ timeout: 10_000 });

    const cs = await label.evaluate((el) => {
      const c = getComputedStyle(el);
      return {
        borderLeftWidth: c.borderLeftWidth,
        borderLeftStyle: c.borderLeftStyle,
        paddingLeft: c.paddingLeft,
      };
    });
    expect(cs.borderLeftWidth).toBe('0px');
    expect(cs.borderLeftStyle).toBe('none');
    expect(cs.paddingLeft).toBe('0px');

    // The icon + "ORDERS" text must still render side by side — this
    // fix must not have broken the shared class's layout properties.
    await expect(label).toContainText('Orders');
    await expect(label.locator('.oes-modal-name-icon')).toBeVisible();
  });
});
