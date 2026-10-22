/**
 * derivatives_payoff_draft_toggle.spec.js
 *
 * 2026-10 draft UX round 2 — the per-underlying DRAFT ON / DRAFT OFF toggle in
 * the Payoff card header (`.payoff-draft-toggle`, derivatives +page.svelte).
 *   1. Absent when the viewed underlying has no draft leg (no draft at all).
 *   2. Absent when the only draft belongs to a DIFFERENT underlying (scoping).
 *   3. Appears once a draft exists for the viewed underlying, defaults ON
 *      (`.is-on`, text "DRAFT ON"), toggles OFF/ON, and the draft row stays
 *      rendered in the Legs grid the whole time (toggle never hides a row).
 *
 * Assertion scope: the payoff curve / Greeks / Snapshot values are not exposed
 * with stable DOM selectors, so inclusion is asserted via the toggle's own
 * state + title and via the Legs row staying put. Numeric payoff deltas are
 * not asserted here (see the final report).
 *
 * All /api/orders/drafts traffic goes through fixtures/draftsMock.js.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { mockDrafts } from './fixtures/draftsMock.js';
import { skipUnlessRendered } from './fixtures/instrumentGate.js';

const DERIV = '/admin/derivatives?u=NIFTY';
const NIFTY_ROWS = [
  { id: 9001, symbol: 'NIFTY26OCTFUT', exchange: 'NFO', transaction_type: 'BUY',
    quantity: 75, initial_price: 25000, account: 'ZG0790' },
  { id: 9002, symbol: 'NIFTY26NOVFUT', exchange: 'NFO', transaction_type: 'SELL',
    quantity: 75, initial_price: 25200, account: 'ZG0790' },
];
const CRUDE_ONLY_ROWS = [
  { id: 9002, symbol: 'CRUDEOIL26NOVFUT', exchange: 'MCX', transaction_type: 'SELL',
    quantity: 100, initial_price: 6500, account: 'ZG0790' },
];
const LEG_WAIT = 200_000;

const toggle = (page) => page.locator('.payoff-draft-toggle');
const draftLeg = (page, tag) => page.locator('.cand-row.cand-draft', { hasText: tag }).first();

/** Wait until the page shell is up (underlying dropdown rendered). */
async function waitForShell(page) {
  await expect(page.locator('button#opt-und')).toBeVisible({ timeout: 20_000 });
}

test.describe('Derivatives Payoff — per-underlying DRAFT toggle', () => {
  test('1: toggle absent when there is no draft at all', async ({ page }) => {
    test.setTimeout(120_000);
    await loginAsAdmin(page);
    await mockDrafts(page, []);
    await page.goto(DERIV, { waitUntil: 'domcontentloaded', timeout: 30_000 });
    await waitForShell(page);
    await expect(toggle(page)).toHaveCount(0);
  });

  test('2: toggle absent when the only draft belongs to a different underlying', async ({ page }) => {
    test.setTimeout(120_000);
    await loginAsAdmin(page);
    await mockDrafts(page, CRUDE_ONLY_ROWS);
    await page.goto(DERIV, { waitUntil: 'domcontentloaded', timeout: 30_000 });
    await waitForShell(page);
    // Viewing NIFTY: the CRUDEOIL draft is not in this underlying's candidates.
    await expect(toggle(page)).toHaveCount(0);
  });

  test('3: appears for the viewed underlying, defaults ON, toggles OFF/ON, Legs row never hidden', async ({ page }) => {
    test.setTimeout(260_000);
    await loginAsAdmin(page);
    await mockDrafts(page, NIFTY_ROWS);
    await page.goto(DERIV, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    const leg = draftLeg(page, 'OCT26');
    await skipUnlessRendered(leg, 'OCT26 draft leg');

    // Button appears once the NIFTY draft leg exists.
    await expect(toggle(page)).toBeVisible({ timeout: 15_000 });
    // Defaults ON.
    await expect(toggle(page)).toHaveClass(/is-on/);
    await expect(toggle(page)).toHaveText(/DRAFT ON/);
    await expect(toggle(page)).toHaveAttribute('title', /count toward payoff\/Greeks\/Snapshot/);

    // Toggle OFF: excluded from payoff/Greeks/Snapshot ...
    await toggle(page).click();
    await expect(toggle(page)).not.toHaveClass(/is-on/);
    await expect(toggle(page)).toHaveText(/DRAFT OFF/);
    await expect(toggle(page)).toHaveAttribute('title', /excluded from payoff\/Greeks\/Snapshot/);
    // ... but the draft row itself stays in the Legs grid (scope boundary).
    await expect(leg).toBeVisible();
    await expect(draftLeg(page, 'NOV26')).toBeVisible();

    // Toggle back ON.
    await toggle(page).click();
    await expect(toggle(page)).toHaveClass(/is-on/);
    await expect(toggle(page)).toHaveText(/DRAFT ON/);
    await expect(leg).toBeVisible();
  });
});
