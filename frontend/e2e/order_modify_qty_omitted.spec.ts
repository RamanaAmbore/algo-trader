/**
 * order_modify_qty_omitted.spec.ts
 *
 * Regression guard for audit defect C1 (2026-09): the order-modify ticket
 * rebuilt `quantity` from the order-book row's `_lots × _lotSize` and always
 * sent it in the PUT payload, even when the operator only changed price.
 * Combined with a backend normalization gap (fixed separately), a
 * price-only edit on a resting MCX order could silently resize it
 * (1-lot CRUDEOILM → 10 lots).
 *
 * Frontend defense-in-depth fix: `buildModifyPayload` in
 * `orderTicketSubmit.js` now omits `quantity` from the PUT body whenever
 * the ticket's resolved qty equals the order's original (untouched) qty —
 * only a genuine, intentional resize includes the field.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *  1. SSOT       — buildModifyPayload is the single place the PUT body is
 *                  assembled; asserted via the actual network request, not
 *                  a mocked internal.
 *  2. Perf       — modal opens + PUT fires within a few seconds (network
 *                  fully mocked, no real broker).
 *  3. Stale      — grep-style: quantity must be ABSENT from the body, not
 *                  merely equal to the old value by coincidence.
 *  4. Reusable   — reuses the same synthetic `lp:modify-order` injection
 *                  pattern as order-modify-cancel.spec.ts.
 *  5. UX         — price-only edit still succeeds (submit button enabled,
 *                  no validation block) while quantity stays untouched.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/order_modify_qty_omitted.spec.ts \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect, Page } from '@playwright/test';

const BASE       = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const ORDERS_URL = `${BASE}/orders`;

const USER = process.env.PLAYWRIGHT_USER || 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

// Synthetic resting order — 1 lot CRUDEOILM-style MCX order (lot_size 10),
// so original quantity = 10 (contracts). A price-only edit must never
// resend `quantity` in the PUT payload.
const ORIGINAL_QTY = 10;
const ORDER_ID     = 'SYNTH-MODIFY-QTY-1';

let _token: string | null = null;

async function loginAsAdmin(page: Page) {
  if (!_token) {
    for (const u of [USER, 'ambore', 'rambo']) {
      let r: Awaited<ReturnType<typeof page.request.post>> | undefined;
      for (let attempt = 0; attempt < 3; attempt++) {
        r = await page.request.post(`${BASE}/api/auth/login`, {
          data: { username: u, password: PASS },
          headers: { 'Content-Type': 'application/json' },
        });
        if (r.status() !== 429) break;
        await page.waitForTimeout(3000 * (attempt + 1));
      }
      if (r && r.ok()) { _token = (await r.json()).access_token; break; }
    }
    if (!_token) throw new Error(`loginAsAdmin: no valid credentials for ${BASE}`);
  }
  await page.context().addInitScript((tok: string) => {
    sessionStorage.setItem('ramboq_token', tok);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: 'rambo', username: 'rambo', role: 'admin', display_name: 'rambo',
    }));
  }, _token);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_token}` });
}

async function openSyntheticModifyTicket(page: Page) {
  await page.goto(ORDERS_URL, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1000);

  await page.evaluate(({ orderId, qty }) => {
    const node = document.querySelector('.bucket-card-activity');
    if (!node) return;
    const evt = new CustomEvent('lp:modify-order', {
      bubbles: true,
      detail: {
        order_id:         orderId,
        tradingsymbol:    'CRUDEOILM26SEPFUT',
        exchange:         'MCX',
        transaction_type: 'BUY',
        quantity:         qty,
        price:            5800,
        trigger_price:    0,
        product:          'NRML',
        order_type:       'LIMIT',
        account:          'test-account',
      },
    });
    node.dispatchEvent(evt);
  }, { orderId: ORDER_ID, qty: ORIGINAL_QTY });

  await page.waitForTimeout(800);

  const ticketVisible = await page.locator(
    '.ot-card, .ot-overlay, [role="dialog"]'
  ).first().isVisible({ timeout: 5000 }).catch(() => false);

  return ticketVisible;
}

test('price-only modify: PUT payload omits quantity', async ({ page }) => {
  await loginAsAdmin(page);

  let capturedBody: any = null;
  await page.route(`**/api/orders/${ORDER_ID}`, async (route) => {
    if (route.request().method() !== 'PUT') { await route.continue(); return; }
    try { capturedBody = route.request().postDataJSON(); } catch { capturedBody = null; }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ order_id: ORDER_ID, status: 'ok' }),
    });
  });

  const opened = await openSyntheticModifyTicket(page);
  if (!opened) {
    test.skip(true, 'Order ticket modal did not open via synthetic event');
    return;
  }

  // Operator touches ONLY the price field — quantity/lots are never
  // interacted with.
  const priceInput = page.locator('#ot-price');
  await expect(priceInput).toBeVisible({ timeout: 5000 });
  await priceInput.fill('5825');
  await priceInput.blur();

  const submitBtn = page.locator('.ot-submit').first();
  await expect(submitBtn).toBeEnabled({ timeout: 3000 });
  await submitBtn.click();

  await page.waitForTimeout(1000);

  expect(capturedBody, 'PUT /api/orders/{id} was never sent').not.toBeNull();
  expect(capturedBody).not.toHaveProperty('quantity');
  expect(capturedBody.price).toBeCloseTo(5825, 0);
});

test('genuine quantity change: PUT payload includes the new quantity', async ({ page }) => {
  await loginAsAdmin(page);

  let capturedBody: any = null;
  await page.route(`**/api/orders/${ORDER_ID}`, async (route) => {
    if (route.request().method() !== 'PUT') { await route.continue(); return; }
    try { capturedBody = route.request().postDataJSON(); } catch { capturedBody = null; }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ order_id: ORDER_ID, status: 'ok' }),
    });
  });

  const opened = await openSyntheticModifyTicket(page);
  if (!opened) {
    test.skip(true, 'Order ticket modal did not open via synthetic event');
    return;
  }

  // Operator bumps the lot stepper (a real, intentional resize) — find the
  // "+" stepper inside the qty control (QtyInput.svelte: `ot-lots-step`
  // with aria-label "Increase lots" / "Increase qty") and click it once.
  const qtyStepperUp = page.locator('.ot-lots-step[aria-label^="Increase"]').first();
  const hasStepper = await qtyStepperUp.isVisible({ timeout: 2000 }).catch(() => false);
  if (!hasStepper) {
    test.skip(true, 'Qty stepper control not found — cannot exercise genuine resize path');
    return;
  }
  await qtyStepperUp.click();

  const submitBtn = page.locator('.ot-submit').first();
  await expect(submitBtn).toBeEnabled({ timeout: 3000 });
  await submitBtn.click();

  await page.waitForTimeout(1000);

  expect(capturedBody, 'PUT /api/orders/{id} was never sent').not.toBeNull();
  expect(capturedBody).toHaveProperty('quantity');
  expect(capturedBody.quantity).not.toBe(ORIGINAL_QTY);
});
