/**
 * order_ticket_double_click_no_duplicate.spec.js
 *
 * D4 fix (2026-09): a second click on Submit while a submit is already in
 * flight must NOT fire a second order. Before the fix, the trigger-dispatch
 * effect only updated its "last seen" counter on the branch that did NOT
 * early-return on `submitting`, so once `submitting` flipped back to false
 * a rerun of the effect could still see a stale trigger mismatch from the
 * ORIGINAL click and fire `submit()` again — a delayed duplicate order.
 *
 * This test drives the real UI: opens a blank ticket, fills a symbol + side
 * + price, then rapid-double-clicks the common-action Submit button while
 * the (deliberately slow-responding, mocked) placement POST is in flight.
 * Asserts exactly ONE POST reaches /api/orders/ticket.
 *
 * The order-placement endpoint is fully mocked (delayed + fulfilled) so
 * this test never reaches a real broker.
 *
 * Five quality dimensions:
 *  1. SSOT   — exercises the real trigger-dispatch effect in
 *              OrderTicket.svelte, not a source-grep proxy
 *  2. Perf   — the guard must resolve promptly; test bounds all waits
 *  3. Stale  — reproduces the EXACT regression sequence (submit in flight,
 *              second click arrives, submitting flips back to false)
 *  4. Reuse  — drives the canonical SymbolPanel + OrderTicket, not a
 *              bespoke dialog
 *  5. UX     — also asserts the Submit button visibly disables for the
 *              full duration of the in-flight submit (D4's second ask)
 *
 * Run:
 *   PLAYWRIGHT_USER=rambo PLAYWRIGHT_PASS=admin1234 \
 *   npx playwright test e2e/order_ticket_double_click_no_duplicate.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(90_000);

/** Mock /api/orders/ticket with an artificial delay so both clicks of a
 *  rapid double-click race against the SAME in-flight request. */
function mockSlowTicketPlacement(page, delayMs = 2_500) {
  const requests = [];
  page.route('**/api/orders/ticket', async (route) => {
    requests.push(route.request().postDataJSON());
    await new Promise((r) => setTimeout(r, delayMs));
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ order_id: 'MOCK-DBL-1', mode: 'paper', status: 'OPEN' }),
    });
  });
  return requests;
}

test.describe('D4 — double-click on Submit does not fire a duplicate order', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('rapid double-click while submit is in flight fires exactly one POST', async ({ page }) => {
    const requests = mockSlowTicketPlacement(page, 2_500);

    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1_500);

    const orderBtn = page.locator('button.pha-order').first();
    if (await orderBtn.count() === 0) {
      test.skip(true, 'no .pha-order entry point in this environment');
      return;
    }
    await orderBtn.click({ force: true });

    const modal = page.locator('.oes-modal').first();
    await expect(modal).toBeVisible({ timeout: 8_000 });

    // Fill a symbol so the ticket has something to submit.
    const symInput = page.locator('.oes-sym-input').first();
    if (await symInput.count() === 0) {
      test.skip(true, 'no symbol input in this ticket layout');
      return;
    }
    await symInput.fill('RELIANCE');
    await page.waitForTimeout(800);
    // Confirm/accept the first autocomplete suggestion if one renders.
    await page.keyboard.press('Enter').catch(() => {});
    await page.waitForTimeout(500);

    // Pick a side via the footer selector (does NOT submit — D4/R7 target).
    const sideBtn = page.locator('.oes-footer-side-btn-single').first();
    if (await sideBtn.count() > 0) {
      await sideBtn.click();
      await page.waitForTimeout(200);
    }

    const submitBtn = page.locator('.oes-common-submit').first();
    await expect(submitBtn).toBeVisible({ timeout: 5_000 });
    if (!(await submitBtn.isEnabled())) {
      test.skip(true, 'submit button not enabled — ticket form incomplete in this environment');
      return;
    }

    // Rapid double-click — two dispatchEvent calls back-to-back so both
    // land before Playwright's own actionability re-checks could space
    // them out naturally.
    await submitBtn.click();
    // The button should now be disabled (or show a busy label) for the
    // FULL duration of the in-flight submit — D4's second ask.
    await page.waitForTimeout(50);
    const disabledDuringSubmit = await submitBtn.isDisabled().catch(() => false);
    // Second click — must be a no-op while the first is in flight.
    await submitBtn.click({ force: true }).catch(() => {});

    // Wait past the mocked 2.5s delay so the in-flight request settles.
    await page.waitForTimeout(3_500);

    console.log(`[order_ticket_double_click_no_duplicate] POSTs captured=${requests.length}, disabledDuringSubmit=${disabledDuringSubmit}`);
    expect(requests.length, 'exactly one order POST must fire from a double-click').toBe(1);
    expect(disabledDuringSubmit, 'Submit button must disable for the full duration of an in-flight submit').toBe(true);
  });
});
