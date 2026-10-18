/**
 * position_state_label_info_tooltip.spec.js
 *
 * 2026-10-17 feature, revised same day — operator: "we don't need i for
 * tool tip for st rows in positions and legs. make p1, p2, labels as
 * tool tips. do you think the popup window need to show the pair
 * functionality instead of just showing pair info. clicking on this
 * cell should not interfear with clicking symbol other elements of the
 * row to open order modal. same with legs in derivatives."
 *
 * Original design (same day, superseded): a separate "(i)" icon next to
 * the St-column badge opened a plain-text InfoHint tooltip. Revised
 * design: no separate icon — the badge click (already wired to
 * PositionPinPicker, the REAL pairing-functionality popup) now ALSO
 * carries the generic "what does this state mean" text
 * (positionStateBadge.js's badge.tooltip) as a one-line hint inside that
 * same popup (`.pin-state-hint`). One click target, one popup, both jobs.
 *
 * Covers both surfaces:
 *   - MarketPulse Positions grid (`.pos-state-badge-btn`, ag-Grid
 *     cellRenderer HTML string, routed through MarketPulse.svelte's
 *     central `_tryHandleRowButton` click handler)
 *   - Derivatives Legs grid (`.cand-state-badge-btn`, CandidateLegRow.svelte)
 *
 * Verifies:
 *   1. No separate "(i)" trigger exists in either grid.
 *   2. Clicking the badge opens PositionPinPicker (`.pin-overlay`) and the
 *      popup shows the generic state explanation (`.pin-state-hint`).
 *   3. Clicking the badge does NOT open the order ticket modal
 *      (`.oes-overlay`) — stopPropagation must prevent it bubbling to the
 *      row's own click-to-open-order handler.
 *   4. Clicking elsewhere on the same row (the symbol) still opens the
 *      order ticket modal normally — the badge's stopPropagation must not
 *      leak and suppress unrelated row clicks.
 *
 * Run:
 *   npx playwright test e2e/position_state_label_info_tooltip.spec.js \
 *   --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;
const POSITIONS_RE = /\/api\/positions\/(\?.*)?$/;

const ROWS = [
  {
    tradingsymbol: 'NIFTY26JUN24000CE', symbol: 'NIFTY26JUN24000CE', exchange: 'NFO',
    account: 'ZG0790', product: 'NRML', quantity: 1, average_price: 100, last_price: 110,
    pnl: 10, realised: 0, unrealised: 10, day_pnl: 0, prev_settlement_pnl: 0, close_price: 100,
  },
  {
    tradingsymbol: 'NIFTY26JUN24500CE', symbol: 'NIFTY26JUN24500CE', exchange: 'NFO',
    account: 'ZG0790', product: 'NRML', quantity: -1, average_price: 50, last_price: 40,
    pnl: 10, realised: 0, unrealised: 10, day_pnl: 0, prev_settlement_pnl: 0, close_price: 50,
  },
];

async function mockPositions(page) {
  await page.route(POSITIONS_RE, async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    let body = { rows: [] };
    try {
      const real = await route.fetch();
      body = { ...(await real.json()) };
    } catch (_) { /* backend unreachable — minimal envelope below */ }
    body.rows = ROWS;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

test.describe('MarketPulse Positions grid — St-column badge click', () => {
  test('no separate "(i)" trigger exists', async ({ page }) => {
    await loginAsAdmin(page);
    await mockPositions(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.pos-state-badge-btn').first()).toBeVisible({ timeout: TIMEOUT });
    expect(await page.locator('.pos-state-info-btn').count()).toBe(0);
  });

  test('clicking the badge opens the pin picker with the state explanation, WITHOUT opening the order ticket', async ({ page }) => {
    await loginAsAdmin(page);
    await mockPositions(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const badgeBtn = page.locator('.pos-state-badge-btn').first();
    await expect(badgeBtn).toBeVisible({ timeout: TIMEOUT });
    await badgeBtn.click();

    const pinPicker = page.locator('.pin-overlay');
    await expect(pinPicker).toBeVisible({ timeout: TIMEOUT });
    await expect(pinPicker.locator('.pin-state-hint')).toContainText(/auto-paired/i);

    expect(await page.locator('.oes-overlay').first().isVisible().catch(() => false)).toBe(false);
  });

  test('clicking the symbol on the same row still opens the order ticket normally', async ({ page }) => {
    await loginAsAdmin(page);
    await mockPositions(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const badgeBtn = page.locator('.pos-state-badge-btn').first();
    await expect(badgeBtn).toBeVisible({ timeout: TIMEOUT });

    // Click the row away from the badge cell (e.g. the symbol cell) —
    // the badge's own stopPropagation must not leak into the rest of the row.
    const row = page.locator('.ag-row', { has: badgeBtn }).first();
    const symCell = row.locator('[col-id="tradingsymbol"], .ag-cell').filter({ hasNotText: '' }).nth(1);
    await symCell.click({ force: true }).catch(() => {});

    const orderModal = page.locator('.oes-overlay');
    await expect(orderModal).toBeVisible({ timeout: TIMEOUT });
  });
});

test.describe('Derivatives Legs grid — St-column badge click', () => {
  test('no separate "(i)" trigger exists', async ({ page }) => {
    await loginAsAdmin(page);
    await mockPositions(page);
    await page.goto('/admin/derivatives?u=NIFTY', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.cand-state-badge-btn').first()).toBeVisible({ timeout: TIMEOUT });
    expect(await page.locator('.cand-state-info-btn').count()).toBe(0);
  });

  test('clicking the badge opens the pin picker with the state explanation, WITHOUT opening the order ticket', async ({ page }) => {
    await loginAsAdmin(page);
    await mockPositions(page);
    await page.goto('/admin/derivatives?u=NIFTY', { waitUntil: 'domcontentloaded' });

    const badgeBtn = page.locator('.cand-state-badge-btn').first();
    await expect(badgeBtn).toBeVisible({ timeout: TIMEOUT });
    await badgeBtn.click();

    const pinPicker = page.locator('.pin-overlay');
    await expect(pinPicker).toBeVisible({ timeout: TIMEOUT });
    await expect(pinPicker.locator('.pin-state-hint')).toContainText(/auto-paired/i);

    expect(await page.locator('.oes-overlay').first().isVisible().catch(() => false)).toBe(false);
  });
});
