/**
 * draft_close_action_guard.spec.js
 *
 * 2026-10 draft simplification regression guard. Drafts now look like real
 * positions on MarketPulse and the derivatives Legs grid (NOT PerformancePage
 * — operator decision: PerformancePage is a public, investor-facing route and
 * drafts are excluded from it entirely), so the one invariant that must never
 * regress is: clicking a draft row opens the ticket in OPEN (promote-to-real)
 * mode, never CLOSE.
 *
 * Asserted on the real wire payload, not on DOM copy: the ticket POST to
 * /api/orders/ticket must carry intent='open' and the draft's own direction.
 * A close would carry intent='close' and the opposite side. Both drafts used
 * here are BUY (NIFTY 75) / SELL (CRUDEOIL 100); the NIFTY draft is the one
 * clicked, so a close-shaped submit would show side='SELL'.
 *
 * Opening the ticket waits on the instrument master (slow on a cold dev
 * context), so each test carries the same long headroom as the existing
 * draft_positions_* specs.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { mockDrafts } from './fixtures/draftsMock.js';
import { skipUnlessRendered } from './fixtures/instrumentGate.js';

const NIFTY_DRAFT_ID = 9001;
const NIFTY_ROWS = [{
  id: NIFTY_DRAFT_ID, symbol: 'NIFTY26OCTFUT', exchange: 'NFO', transaction_type: 'BUY',
  quantity: 75, initial_price: 25000, account: 'ZG0790',
}];

/** Stub the ticket endpoint and record every body it receives. */
async function captureTickets(page) {
  const bodies = [];
  await page.route('**/api/orders/ticket', async (route) => {
    bodies.push(route.request().postDataJSON());
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ status: 'ok', order_id: 1 }),
    });
  });
  // Mock execution mode as paper so the pre-existing "Engine is idle — pick
  // PAPER/SIM/REPLAY" guard doesn't block Submit from ever reaching the
  // ticket endpoint (same pattern as crudeoil_add_sell.spec.js) — unrelated
  // to the draft simplification, this guard predates it.
  await page.route('**/api/admin/execution/mode', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ mode: 'paper', paper_trading_mode: true, dev_active: true }),
    });
  });
  return bodies;
}

/** Assert the one submitted ticket is an OPEN promote of the BUY draft. */
function expectPromoteNotClose(bodies) {
  expect(bodies).toHaveLength(1);
  const body = bodies[0];
  expect(body.intent, 'draft promote must never be classified as a close').not.toBe('close');
  expect(body.intent).toBe('open');
  expect(body.side).toBe('BUY');
}

test.describe('Draft close-action guard — clicking a draft opens promote, never close', () => {
  test('MarketPulse: draft row click submits as OPEN, same side as the draft', async ({ page }) => {
    test.setTimeout(260_000);
    await loginAsAdmin(page);
    await mockDrafts(page, NIFTY_ROWS);
    const bodies = await captureTickets(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: 30_000 });

    const cell = page.locator(
      `.ag-row[row-id^="NIFTY26OCTFUT::draft::${NIFTY_DRAFT_ID}"] .ag-cell[col-id="tradingsymbol"]`,
    ).first();
    await skipUnlessRendered(cell, 'draft symbol cell');
    await cell.click();

    const modal = page.locator('.oes-modal').first();
    await expect(modal).toBeVisible({ timeout: 200_000 });
    const submit = modal.locator('.oes-common-submit').first();
    await expect(submit).toBeEnabled({ timeout: 90_000 });
    await submit.click();

    await expect.poll(() => bodies.length, { timeout: 15_000 }).toBe(1);
    expectPromoteNotClose(bodies);
  });

  test('Derivatives Legs: draft leg click submits as OPEN, same side as the draft', async ({ page }) => {
    test.setTimeout(260_000);
    await loginAsAdmin(page);
    await mockDrafts(page, NIFTY_ROWS);
    const bodies = await captureTickets(page);
    await page.goto('/admin/derivatives?u=NIFTY', { waitUntil: 'domcontentloaded', timeout: 30_000 });

    const leg = page.locator('.cand-row.cand-draft', { hasText: 'OCT26' }).first();
    await skipUnlessRendered(leg, 'OCT26 draft leg');
    await leg.getByText(/OCT26/).first().click();

    const modal = page.locator('.oes-modal').first();
    await expect(modal).toBeVisible({ timeout: 200_000 });
    const submit = modal.locator('.oes-common-submit').first();
    await expect(submit).toBeEnabled({ timeout: 90_000 });
    await submit.click();

    await expect.poll(() => bodies.length, { timeout: 15_000 }).toBe(1);
    expectPromoteNotClose(bodies);
  });
});
