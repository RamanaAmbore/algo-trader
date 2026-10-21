/**
 * draft_positions_derivatives_legs.spec.js
 *
 * 2026-10-21 — draft positions on the derivatives page's Legs grid.
 *   C. Select + "Clear Selected Drafts (N)": the header button counts only
 *      checked draft rows, is disabled at 0, and removes exactly the checked
 *      ids (one DELETE each via payoffDrafts.remove) — unchecked drafts survive.
 *   D. Click-to-promote: clicking a draft row body (not its checkbox) opens
 *      the SymbolPanel ticket restricted to Ticket + Chart tabs, nothing is
 *      sent to the broker until Submit, and a confirmed paper/live submit
 *      removes the originating draft.
 *
 * All /api/orders/drafts traffic goes through fixtures/draftsMock.js.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { mockDrafts } from './fixtures/draftsMock.js';
import { skipUnlessRendered } from './fixtures/instrumentGate.js';

// Both drafts sit on NIFTY. The Legs grid lists only the selected underlying's
// candidates, and `?u=` seeds that selection on mount. Drafts are also dropped
// from candidates until getInstrument() resolves them (buildCandidatePositions),
// so every wait here allows for the slow dev instrument master.
const DERIV = '/admin/derivatives?u=NIFTY';
const ROWS = [
  { id: 9001, symbol: 'NIFTY26OCTFUT', exchange: 'NFO', transaction_type: 'BUY',
    quantity: 75, initial_price: 25000, account: 'ZG0790' },
  { id: 9002, symbol: 'NIFTY26NOVFUT', exchange: 'NFO', transaction_type: 'SELL',
    quantity: 75, initial_price: 25200, account: 'ZG0790' },
];
const LEG_WAIT = 200_000;

/** Legs-grid draft row by expiry tag ('OCT26' -> 9001, 'NOV26' -> 9002). */
function draftLeg(page, expiryTag) {
  return page.locator('.cand-row.cand-draft', { hasText: expiryTag }).first();
}

test.describe('Derivatives Legs — select and clear drafts', () => {
  test('C: header button counts only checked draft legs and removes exactly those', async ({ page }) => {
    test.setTimeout(260_000);
    await loginAsAdmin(page);
    const state = await mockDrafts(page, ROWS);
    await page.goto(DERIV, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    const clearBtn = page.locator('.legs-clear-drafts-btn');
    await expect(clearBtn).toBeVisible({ timeout: LEG_WAIT });
    await expect(clearBtn).toBeDisabled();
    await expect(clearBtn).toHaveText(/Clear Selected Drafts \(0\)/);

    await skipUnlessRendered(draftLeg(page, 'OCT26'), 'OCT26 draft leg');
    await draftLeg(page, 'OCT26').locator('.cand-draft-select').check();
    await expect(clearBtn).toHaveText(/Clear Selected Drafts \(1\)/);
    await expect(clearBtn).toBeEnabled();

    await clearBtn.click();
    await expect.poll(() => state.calls.filter((c) => c.method === 'DELETE').map((c) => c.id), { timeout: 15_000 })
      .toEqual([9001]);
    // Unchecked NOV26 draft survives; the checked OCT26 one is gone.
    await expect(draftLeg(page, 'NOV26')).toBeVisible();
    await expect(page.locator('.cand-row.cand-draft', { hasText: 'OCT26' })).toHaveCount(0);
  });
});

test.describe('Derivatives Legs — click-to-promote a draft', () => {
  test('D: draft leg click opens Ticket+Chart only, sends nothing until Submit, and removes the draft after a confirmed submit', async ({ page }) => {
    // Opening the ticket waits on the instrument master, which is slow on dev
    // (see draft_positions_pulse.spec.js D) — same headroom here.
    test.setTimeout(240_000);
    await loginAsAdmin(page);
    const state = await mockDrafts(page, ROWS);
    const ticketCalls = [];
    await page.route('**/api/orders/ticket', async (route) => {
      ticketCalls.push(route.request().postDataJSON());
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ status: 'ok', order_id: 1 }),
      });
    });
    await page.goto(DERIV, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    const leg = draftLeg(page, 'OCT26');
    await skipUnlessRendered(leg, 'OCT26 draft leg');
    // Click the row body (symbol text), not the checkbox or the × button.
    await leg.getByText(/OCT26/).first().click();

    const modal = page.locator('.oes-modal').first();
    await expect(modal).toBeVisible({ timeout: 200_000 });

    const labels = (await page.getByRole('tab').allTextContents())
      .map((t) => t.trim().toLowerCase().replace(/\s+\d+$/, ''));
    expect(labels).toEqual(expect.arrayContaining(['ticket', 'chart']));
    expect(labels).not.toContain('chain');
    expect(labels).not.toContain('cli');

    expect(ticketCalls).toHaveLength(0);

    const submit = modal.locator('.oes-common-submit').first();
    await expect(submit).toBeEnabled({ timeout: 90_000 });
    await submit.click();

    await expect.poll(() => ticketCalls.length, { timeout: 15_000 }).toBe(1);
    // derivatives onTicketSubmit -> removeDraft('pd_9001') -> payoffDrafts.remove(9001).
    await expect.poll(() => state.calls.filter((c) => c.method === 'DELETE').map((c) => c.id), { timeout: 15_000 })
      .toEqual([9001]);
    await expect(page.locator('.cand-row.cand-draft', { hasText: 'OCT26' })).toHaveCount(0, { timeout: 15_000 });
  });
});
