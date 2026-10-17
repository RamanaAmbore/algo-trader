/**
 * position_state_label_info_tooltip.spec.js
 *
 * 2026-10-17 feature — operator: "make the group name labels as tool
 * tips when clicked to show what they mean."
 *
 * The St-column state badge (GTT/pinned/paired/orphan glyph) is ALREADY
 * clickable in both grids — it opens the PositionPinPicker. Per the
 * general tooltip-label rule set the same session ("if label is plain
 * text anywhere, it should work as a tooltip. otherwise, we need to add
 * circle with i as tooltip"), an already-interactive label gets a
 * SEPARATE small "(i)" icon rather than overloading its own click.
 *
 * Covers both surfaces:
 *   - MarketPulse Positions grid (`.pos-state-info-btn`, ag-Grid
 *     cellRenderer HTML string, routed through MarketPulse.svelte's
 *     central `_tryHandleRowButton` click handler; popup is a real
 *     InfoHint anchored to the clicked DOM element)
 *   - Derivatives Legs grid (`.cand-state-info-btn`, CandidateLegRow.svelte,
 *     a real Svelte component — InfoHint mounted directly with
 *     hideButton+anchor+bind:open)
 *
 * Content is the SAME generic text already on `badge.tooltip`
 * (positionStateBadge.js) — static per state TYPE, not a per-instance
 * dynamic counterparty lookup (operator-confirmed scope).
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

test.describe('MarketPulse Positions grid — St-column info trigger', () => {
  test('clicking the "i" shows the generic state explanation WITHOUT opening the pin/unpin picker', async ({ page }) => {
    await loginAsAdmin(page);
    await mockPositions(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const infoBtn = page.locator('.pos-state-info-btn').first();
    await expect(infoBtn).toBeVisible({ timeout: TIMEOUT });
    await infoBtn.click();

    const popup = page.locator('.info-popout, [class*="popout"]').first();
    await expect(popup).toBeVisible({ timeout: TIMEOUT });
    await expect(popup).toContainText(/auto-paired/i);

    // The pin/unpin picker must NOT have opened — info click is a
    // distinct target from the badge button.
    const pinPicker = page.locator('.pin-overlay');
    expect(await pinPicker.first().isVisible().catch(() => false)).toBe(false);
  });

  test('the badge button itself still opens the pin/unpin picker, unaffected by the info trigger', async ({ page }) => {
    await loginAsAdmin(page);
    await mockPositions(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const badgeBtn = page.locator('.pos-state-badge-btn').first();
    await expect(badgeBtn).toBeVisible({ timeout: TIMEOUT });
    await badgeBtn.click();

    const pinPicker = page.locator('.pin-overlay').first();
    await expect(pinPicker).toBeVisible({ timeout: TIMEOUT });
  });
});

test.describe('Derivatives Legs grid — St-column info trigger', () => {
  test('clicking the "i" shows the generic state explanation', async ({ page }) => {
    await loginAsAdmin(page);
    await mockPositions(page);
    await page.goto('/admin/derivatives?u=NIFTY', { waitUntil: 'domcontentloaded' });

    const infoBtn = page.locator('.cand-state-info-btn').first();
    await expect(infoBtn).toBeVisible({ timeout: TIMEOUT });
    await infoBtn.click();

    const popup = page.locator('.info-popout, [class*="popout"]').first();
    await expect(popup).toBeVisible({ timeout: TIMEOUT });
    await expect(popup).toContainText(/auto-paired/i);
  });

  test('the badge button itself still opens the pin/unpin picker, unaffected by the info trigger', async ({ page }) => {
    await loginAsAdmin(page);
    await mockPositions(page);
    await page.goto('/admin/derivatives?u=NIFTY', { waitUntil: 'domcontentloaded' });

    const badgeBtn = page.locator('.cand-state-badge-btn').first();
    await expect(badgeBtn).toBeVisible({ timeout: TIMEOUT });
    await badgeBtn.click();

    const pinPicker = page.locator('.pin-overlay').first();
    await expect(pinPicker).toBeVisible({ timeout: TIMEOUT });
  });
});
