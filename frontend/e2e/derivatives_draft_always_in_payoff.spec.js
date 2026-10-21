/**
 * derivatives_draft_always_in_payoff.spec.js
 *
 * 2026-10 draft simplification: the `showDraftInPayoff` toggle is GONE. A draft
 * leg feeds the payoff / Greeks aggregate (POST /api/options/strategy-analytics)
 * with NO toggle interaction — it is included purely because its own per-row
 * enable checkbox (`enabledSymbols` / `_isLegEnabled`) is checked, exactly like
 * any real leg. Unchecking that row's box must drop it from the payload.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { mockDrafts } from './fixtures/draftsMock.js';
import { skipUnlessRendered } from './fixtures/instrumentGate.js';

const DERIV = '/admin/derivatives?u=NIFTY';
// Two drafts so C2 can uncheck one and still prove a fresh payoff POST goes out
// (with zero enabled legs the analytics request may not fire at all).
// The OCT draft's price is deliberately odd so it cannot collide with a real
// NIFTY position held on the dev account: a leg is matched on symbol AND avg_cost.
const DRAFT_SYMBOL = 'NIFTY26OCTFUT';
const DRAFT_AVG = 24777;
const DRAFT_QTY = 75;
const OTHER_SYMBOL = 'NIFTY26NOVFUT';
const ROWS = [
  { id: 9301, symbol: DRAFT_SYMBOL, exchange: 'NFO', transaction_type: 'BUY',
    quantity: DRAFT_QTY, initial_price: DRAFT_AVG, account: 'ZG0790' },
  { id: 9302, symbol: OTHER_SYMBOL, exchange: 'NFO', transaction_type: 'SELL',
    quantity: DRAFT_QTY, initial_price: 25200, account: 'ZG0790' },
];

/** OCT26 draft leg row in the Legs grid. */
function draftLeg(page) {
  return page.locator('.cand-row.cand-draft', { hasText: 'OCT26' }).first();
}

/** Per-row enable checkbox — the one that is NOT the draft-select bulk box. */
function enableBox(page) {
  return draftLeg(page).locator('input[type="checkbox"]:not(.cand-draft-select)').first();
}

/**
 * Record every strategy-analytics payload. Returns helpers to read the latest
 * one and to find the draft leg inside it.
 */
function trackAnalytics(page) {
  const payloads = [];
  page.on('request', (req) => {
    if (req.url().includes('/options/strategy-analytics') && req.method() === 'POST') {
      try { payloads.push(req.postDataJSON()); } catch (_) { /* non-JSON: ignore */ }
    }
  });
  // Match on symbol AND avg_cost: a real position on the same symbol would
  // carry a different cost, so it can never satisfy this lookup.
  const latestDraftLeg = () => {
    const last = payloads[payloads.length - 1];
    return (last?.legs || []).find((l) =>
      String(l.symbol).toUpperCase() === DRAFT_SYMBOL && Number(l.avg_cost) === DRAFT_AVG) || null;
  };
  const latestLegSymbols = () => {
    const last = payloads[payloads.length - 1];
    return (last?.legs || []).map((l) => `${String(l.symbol).toUpperCase()}@${Number(l.avg_cost)}`);
  };
  return { payloads, latestDraftLeg, latestLegSymbols };
}

test.describe('Derivatives payoff — draft legs always count, no toggle', () => {
  test('C1: draft leg is in the payoff/Greeks request with no toggle interaction', async ({ page }) => {
    test.setTimeout(260_000);
    await loginAsAdmin(page);
    await mockDrafts(page, ROWS);
    const { latestDraftLeg, payloads } = trackAnalytics(page);
    await page.goto(DERIV, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    // The draft leg renders, its enable box is checked by default, and there is
    // no DRAFT / "Incl. draft" toggle anywhere on the page.
    await skipUnlessRendered(draftLeg(page), 'OCT26 draft leg');
    await expect(enableBox(page)).toBeChecked();
    await expect(page.locator('.legend-toggle-draft')).toHaveCount(0);
    await expect(page.getByText(/Incl\.? draft/i)).toHaveCount(0);

    await expect.poll(() => latestDraftLeg(), { timeout: 60_000 }).not.toBeNull();
    expect(latestDraftLeg().qty).toBe(DRAFT_QTY);
    expect(payloads.length).toBeGreaterThan(0);
  });

  test('C2: unchecking the OCT draft drops only it from the payoff request; re-checking restores it', async ({ page }) => {
    test.setTimeout(260_000);
    await loginAsAdmin(page);
    await mockDrafts(page, ROWS);
    const { latestDraftLeg, latestLegSymbols } = trackAnalytics(page);
    await page.goto(DERIV, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    await skipUnlessRendered(draftLeg(page), 'OCT26 draft leg');
    await expect.poll(() => latestDraftLeg(), { timeout: 60_000 }).not.toBeNull();

    await enableBox(page).uncheck();
    // The NOV draft is still enabled, so a fresh POST must still go out — and
    // it must carry NOV but no longer the OCT draft.
    await expect.poll(() => latestLegSymbols(), { timeout: 30_000 })
      .toEqual(expect.arrayContaining([expect.stringMatching(new RegExp(`^${OTHER_SYMBOL}@`))]));
    await expect.poll(() => latestDraftLeg(), { timeout: 30_000 }).toBeNull();

    await enableBox(page).check();
    await expect.poll(() => latestDraftLeg(), { timeout: 30_000 }).not.toBeNull();
  });
});
