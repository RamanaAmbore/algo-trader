/**
 * orders_title_and_modal_label_cleanup.spec.js — 2026-10-13, operator asks:
 * (1) /orders page title (next to the timestamp) renamed "Order entry" ->
 *     "Orders".
 * (2) SymbolPanel's order-ticket modal "Order entry" label (.oes-modal-name)
 *     gained the same border-left + left-padding decoration every other
 *     header label already has (.ch-title, .mp-bucket-label, orders/
 *     +page.svelte's own .oc-entry-label), keeping its deliberate amber
 *     color.
 * (3) The order-slip SVG icon was briefly removed the same day, then
 *     restored (operator: "keep the icon back to orders modal and
 *     page") to match orders/+page.svelte's own .oc-entry-icon, which
 *     was never removed — the modal was the one that drifted, not the
 *     page.
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

test.describe('Orders page title + SymbolPanel modal label cleanup', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('/orders page title reads "Orders", not "Order entry"', async ({ page }) => {
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    const title = page.locator('.page-title-chip').first();
    await expect(title).toBeVisible({ timeout: 15_000 });
    await expect(title).toHaveText('Orders');
  });

  test('SymbolPanel order-ticket modal: "Order entry" label has its icon and border-left decoration', async ({ page }) => {
    // /orders hides the global Order trigger (PageHeaderActions
    // hideOrder={true} — the whole page is already an order-entry
    // surface via its own inline Order Entry card), so open the modal
    // from /pulse instead. Click the header's own Order button
    // (.pha-order, PageHeaderActions.svelte) directly rather than the
    // 't' keyboard shortcut or a live position row — both proved
    // unreliable/book-dependent; the button is always present and not
    // gated on any live data.
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    const orderBtn = page.locator('.pha-order');
    await expect(orderBtn).toBeVisible({ timeout: 15_000 });
    await orderBtn.click();
    const modal = page.locator('.canonical-modal-overlay').first();
    await expect(modal, 'order ticket modal should open').toBeVisible({ timeout: 10_000 });

    const label = modal.locator('.oes-modal-name').first();
    await expect(label).toBeVisible({ timeout: 5_000 });
    await expect(label).toHaveText('Order entry');

    // Icon restored (2026-10-13, same day) — matches the page's own
    // .oc-entry-icon, which was never removed.
    await expect(label.locator('svg.oes-modal-name-icon')).toHaveCount(1);
    await expect(modal.locator('.oes-modal-name-icon')).toHaveCount(1);

    // Border-left decoration present, color unchanged (amber).
    const style = await label.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { borderLeftWidth: cs.borderLeftWidth, borderLeftStyle: cs.borderLeftStyle, color: cs.color, borderLeftColor: cs.borderLeftColor };
    });
    expect(parseFloat(style.borderLeftWidth)).toBeGreaterThan(0);
    expect(style.borderLeftStyle).toBe('solid');
    // currentColor means border-left-color resolves to the same value as color.
    expect(style.borderLeftColor).toBe(style.color);
  });
});
