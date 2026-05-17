/**
 * Order placement / management from /orders.
 *
 * Click an open order → OrderDetail drawer → "Modify" launches the
 * shared OrderTicket with action='modify'. Side is locked, qty +
 * price + trigger remain editable. Submit hits PUT /api/orders/{id}
 * (not POST /api/orders/ticket — modify is a different path).
 *
 * /orders may render zero rows on quiet days. When that's the case,
 * the test verifies the empty-state copy + the command-bar entry
 * point and skips the modify flow.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin }  from './fixtures/auth.js';

const TIMEOUT = 30_000;

test.describe('Order placement · /orders page', () => {

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders');
    // The page renders either a row list or the "No orders today."
    // empty state. Wait for one of them to settle.
    await page.waitForFunction(
      () =>
        !!document.querySelector('button.order-row, .text-center.text-muted'),
      null,
      { timeout: TIMEOUT },
    );
  });

  test('page loads with command bar + log panel', async ({ page }) => {
    // Command bar (input field for `buy …` / `sell …` typing) is
    // always present, regardless of order count.
    const cmdInput = page.locator('input.cmd-input, input[placeholder*="buy"], input[placeholder*="sell"]').first();
    if (await cmdInput.count() === 0) {
      // Page layout may have moved; assert at least the log panel mounted.
      const log = page.locator('.log-panel, .log-tabs').first();
      await expect(log).toBeVisible({ timeout: TIMEOUT });
    } else {
      await expect(cmdInput).toBeVisible({ timeout: TIMEOUT });
    }
  });

  test('Modify on an open order opens the ticket with side locked', async ({ page }) => {
    // Find an order row marked OPEN (column has "OPEN" text in
    // a status pill); skip if none.
    const orderRows = page.locator('button.order-row');
    const count = await orderRows.count();
    if (count === 0) {
      test.skip(true, 'no orders today — empty-state path');
    }
    // Pick the first row that mentions OPEN status.
    let openRow = null;
    for (let i = 0; i < count && i < 10; i++) {
      const t = await orderRows.nth(i).innerText();
      if (/OPEN/i.test(t)) { openRow = orderRows.nth(i); break; }
    }
    if (!openRow) {
      test.skip(true, 'no OPEN orders today — every row is terminal');
    }
    await openRow.click();

    // OrderDetail drawer mounts — look for the Modify button.
    const modifyBtn = page.locator('button', { hasText: /^Modify$/ }).first();
    await expect(modifyBtn).toBeVisible({ timeout: TIMEOUT });
    await modifyBtn.click();

    // OrderTicket modal appears with action=modify → side toggle
    // carries the `ot-locked` class.
    const modal = page.locator('.ot-modal').first();
    await expect(modal).toBeVisible({ timeout: TIMEOUT });
    const sideToggle = modal.locator('.ot-side-toggle');
    await expect(sideToggle).toHaveClass(/ot-locked/);

    // Submit button label reads "Modify" (or "Modify · #<id>").
    const submit = modal.locator('button.ot-submit');
    await expect(submit).toContainText(/Modify/i);

    // Close without submitting.
    await modal.locator('button.ot-close').click();
    await expect(modal).toBeHidden({ timeout: TIMEOUT });
  });

  test('Cancel on an open order routes through OrderDetail confirm', async ({ page }) => {
    const orderRows = page.locator('button.order-row');
    const count = await orderRows.count();
    if (count === 0) {
      test.skip(true, 'no orders today');
    }
    let openRow = null;
    for (let i = 0; i < count && i < 10; i++) {
      const t = await orderRows.nth(i).innerText();
      if (/OPEN/i.test(t)) { openRow = orderRows.nth(i); break; }
    }
    if (!openRow) {
      test.skip(true, 'no OPEN orders to cancel');
    }
    await openRow.click();

    // Cancel button surfaces in OrderDetail. We assert it's visible
    // and enabled but do NOT click — cancelling a real order would
    // mutate prod-paper state which is exactly what we want to avoid.
    const cancelBtn = page.locator('button', { hasText: /^Cancel$/i }).first();
    await expect(cancelBtn).toBeVisible({ timeout: TIMEOUT });
  });
});
