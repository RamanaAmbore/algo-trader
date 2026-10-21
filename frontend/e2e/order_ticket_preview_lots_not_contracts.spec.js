/**
 * order_ticket_preview_lots_not_contracts.spec.js
 *
 * 2026-10-20 fix (plan item 7, Ticket-tab half) — `OrderTicket.svelte`'s
 * template-preview `$effect` (debounced call to `previewTicketTemplate`
 * / `POST /api/orders/ticket/preview`) sent `quantity: Number(_qty)` —
 * `_qty` is already CONTRACTS (`_qty = _lots * _lotSize`, the sync
 * effect a few lines above this one). The backend's ticket-preview
 * endpoint (`_rco_preview_resolve_qty` in
 * `backend/api/routes/orders.py`) reads `quantity` as LOTS for F&O
 * exchanges and multiplies by lot_size itself. Sending contracts
 * doubled the lot multiplier server-side — `parent_qty` became
 * `lot_size²` contracts instead of `lot_size` contracts, inflating the
 * wing quantity and every TP/SL/margin preview chip derived from the
 * plan for any templated/bracket order. Fixed: send `_lots` for F&O
 * (`_lotSize > 0`), matching `OptionChainTab.svelte`'s existing
 * `leg.lots`-only convention for the same endpoint; equity (no lot
 * size) is unaffected and still sends raw `_qty`.
 *
 * Assertion strategy: rather than hardcoding a lot_size value (which
 * drifts), the test sets lots to a known value (2) via the real UI
 * stepper and asserts the captured request's `quantity` equals that
 * exact lot count. Pre-fix this would have been `2 * lotSize`
 * (NIFTY's real lot_size, never 1) instead of `2` — the two values can
 * never coincide for a real F&O instrument, so this discriminates the
 * bug without needing to know the exact lot size.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

async function mockPaperMode(page) {
  await page.route('**/api/admin/execution/mode', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          mode: 'paper',
          branch: 'dev',
          allowed_modes: ['idle', 'sim', 'replay', 'paper', 'shadow'],
        }),
      });
    } else {
      await route.continue();
    }
  });
}

async function _seedNiftyOnTicket(page) {
  await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const symInput = page.locator('.ssi-input').first();
  await expect(symInput).toBeVisible({ timeout: 15_000 });
  await symInput.fill('NIFTY');
  const sugg = page.locator('.ssi-drop .ssi-row').first();
  await expect(sugg).toBeVisible({ timeout: 10_000 });
  await sugg.click({ force: true });
  await page.waitForTimeout(500);
}

test.describe('Order Ticket — template preview sends lots, not contracts', () => {
  test('live: increasing lots to 2 on an F&O symbol sends quantity=2 (lots) to /api/orders/ticket/preview, not 2*lotSize', async ({ page }) => {
    test.setTimeout(60_000);
    let previewBody = null;
    let sawPreviewCall = false;
    await page.route('**/api/orders/ticket/preview', async (route) => {
      sawPreviewCall = true;
      previewBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ plan: { template_id: 1, template_name: 'Test', parent_qty: previewBody.quantity, wing: null } }),
      });
    });
    await mockPaperMode(page);
    await loginAsAdmin(page);
    await _seedNiftyOnTicket(page);

    // Activate the Bracket toggle — the template-preview effect only
    // fires once templateId (or a tp/sl override) is non-null.
    const toggle = page.locator('.oes-tpl-button').first();
    await expect(toggle).toBeVisible({ timeout: 15_000 });
    if (!(await toggle.evaluate((el) => el.classList.contains('active')))) {
      await toggle.click();
    }
    await expect(toggle).toHaveClass(/active/, { timeout: 5_000 });

    // Confirm this is a real F&O (lot-based) ticket, not an equity one
    // — the lots stepper only renders when lotSize > 0.
    const lotsInput = page.locator('#ot-lots');
    await expect(lotsInput).toBeVisible({ timeout: 10_000 });

    const increaseLots = page.locator('button[aria-label="Increase lots"]').first();
    await expect(increaseLots).toBeVisible({ timeout: 5_000 });
    await increaseLots.click();
    await page.waitForTimeout(1200); // 200ms debounce + request round trip

    expect(sawPreviewCall, 'expected at least one /api/orders/ticket/preview request').toBe(true);
    expect(previewBody, 'preview request body must have been captured').not.toBeNull();
    expect(previewBody.quantity).toBe(2);
  });
});
