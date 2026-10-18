/**
 * derivatives_legs_underlying_anchor.spec.js
 *
 * 2026-10-18 — operator: "legs should be in sync with it [positions]"
 * (MarketPulse's Positions grid already shows the underlying even when
 * it isn't itself a held position, via `mergeUnderlyingAnchors`). This
 * adds the same concept to the derivatives page's Legs grid: a read-only
 * row for the selected underlying's own spot, always leading its own
 * CE/PE legs (`legsUnderlyingAnchor` in +page.svelte, rendered via the
 * new `.cand-row-underlying-anchor` row, BEFORE the `{#each
 * displayedCandidates}` loop).
 *
 * Deliberately NOT merged into `displayedCandidates`/`candidatePositions`
 * — those arrays feed the TOTAL row, the checkbox/enabled-leg state, the
 * payoff curve's leg set, and the CSV export. This test's core job is
 * proving the anchor row is purely additive at the DOM level: it renders
 * first, carries no checkbox, and the TOTAL row's P&L is unaffected by
 * its presence (computed only from the two real legs below it).
 *
 * Run:
 *   npx playwright test e2e/derivatives_legs_underlying_anchor.spec.js
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(60_000);

const UNDERLYING = 'NIFTY';
const CE_SYMBOL   = 'NIFTY26DEC25000CE';
const PE_SYMBOL   = 'NIFTY26DEC24000PE';
const EXPIRY      = '2026-12-31';
const ACCOUNT     = 'ZG0790';
const DERIV_URL    = `/admin/derivatives?u=${UNDERLYING}`;

function _row(sym, strike, optType, qty, pnl, dayPnl) {
  return {
    tradingsymbol: sym, symbol: sym, exchange: 'NFO',
    quantity: qty, opening_quantity: qty, overnight_quantity: qty,
    day_buy_quantity: 0, day_sell_quantity: 0, day_sell_value: 0,
    average_price: 200.0, last_price: 210.0, close_price: 195.0,
    pnl, day_change_val: dayPnl, unrealised_pnl: pnl, realised_pnl: 0,
    account: ACCOUNT, source: 'live',
  };
}

async function _mockEndpoints(page) {
  await page.route('**/api/positions**', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        rows: [
          _row(CE_SYMBOL, 25000, 'CE', 50, 500, 50),
          _row(PE_SYMBOL, 24000, 'PE', 50, 300, 30),
        ],
        summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null,
      }),
    });
  });
  await page.route('**/api/instruments**', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        cycle_date: '2026-09-29', count: 2,
        items: [
          { s: CE_SYMBOL, e: 'NFO', t: 'CE', ls: 50, ts: 0.05, u: UNDERLYING, x: EXPIRY, k: 25000 },
          { s: PE_SYMBOL, e: 'NFO', t: 'PE', ls: 50, ts: 0.05, u: UNDERLYING, x: EXPIRY, k: 24000 },
        ],
      }),
    });
  });
  await page.route('**/api/holdings**', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ rows: [], summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null }),
    });
  });
  await page.route('**/api/watchlist**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ watchlists: [], symbols: [] }) });
  });
  await page.route('**/api/options/strategy-analytics**', (route) => {
    const body = route.request().postDataJSON();
    const _greeks = { delta: 0.5, gamma: 0.01, theta: -0.3, vega: 0.2, rho: 0.01 };
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        underlying: UNDERLYING, spot: 24500, spot_prev_close: 24300,
        legs: (body?.legs ?? []).map(l => ({ ...l, greeks: _greeks })),
        payoff: [{ spot: 24000, today_value: -100, expiry_value: -100 }],
        risk: { breakevens: [24300] }, breakevens: [24300],
        pnl_at_spot: 800, exp_pnl_at_spot: 800,
        dte: 15, iv: 0.18, aggregate_greeks: _greeks,
      }),
    });
  });
  await page.route('**/api/quotes/stream', async (route) => {
    route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'event: heartbeat\ndata: {}\n\n' });
  });
}

test.describe('Derivatives Legs grid — underlying-anchor row (sync with Positions)', () => {
  test('the underlying-anchor row renders first, before the real CE/PE legs', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const legsGrid = page.locator('.cand-grid');
    await expect(legsGrid).toBeVisible({ timeout: 15_000 });
    const anchor = page.locator('.cand-body-rows > .cand-row-underlying-anchor');
    await expect(anchor).toBeVisible({ timeout: 15_000 });

    // The anchor must be the FIRST row in the body — before any real leg.
    const firstChild = page.locator('.cand-body-rows > div').first();
    await expect(firstChild).toHaveClass(/cand-row-underlying-anchor/);

    // Symbol cell shows the underlying, not a specific option contract.
    await expect(anchor.locator('.cand-anchor-label')).toContainText('NIFTY');
  });

  test('the anchor row carries no checkbox — never enters the enabled-leg/payoff/TOTAL pipeline', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const anchor = page.locator('.cand-body-rows > .cand-row-underlying-anchor');
    await expect(anchor).toBeVisible({ timeout: 15_000 });
    await expect(anchor.locator('input[type="checkbox"]')).toHaveCount(0);
  });

  test('TOTAL P&L reflects only the two real legs (500 + 300 = 800), unaffected by the anchor row', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const totalRow = page.locator('.cand-row.cand-row-total');
    await expect(totalRow).toBeVisible({ timeout: 15_000 });
    // P&L TOTAL cell is the 12th span (index 11) per the grid's fixed
    // column order — matches the existing TOTAL-row convention used by
    // derivatives_total_expclose.spec.js.
    const pnlCell = totalRow.locator('> span').nth(11);
    await expect(pnlCell).toContainText('800');
  });

  test('a real CE/PE leg row still renders its own checkbox (negative control — only the anchor is checkbox-less)', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const realRow = page.locator('.cand-body-rows > .cand-row:not(.cand-row-underlying-anchor):not(.cand-row-total)').first();
    await expect(realRow).toBeVisible({ timeout: 15_000 });
    await expect(realRow.locator('input[type="checkbox"]')).toHaveCount(1);
  });

  test('the anchor row\'s St column shows "X" (2026-10-18, operator: "make st as x, with similar coding like o")', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const anchor = page.locator('.cand-body-rows > .cand-row-underlying-anchor');
    await expect(anchor).toBeVisible({ timeout: 15_000 });
    const stCell = anchor.locator('.cand-state-cell');
    await expect(stCell).toHaveCount(1);
    await expect(stCell).toHaveText('X');
  });

  test('the anchor row\'s symbol cell has a gray right-edge divider, like a real leg\'s own symbol-cell divider', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const anchor = page.locator('.cand-body-rows > .cand-row-underlying-anchor');
    await expect(anchor).toBeVisible({ timeout: 15_000 });
    const label = anchor.locator('.cand-anchor-label');
    const boxShadow = await label.evaluate((el) => getComputedStyle(el).boxShadow);
    expect(boxShadow).not.toBe('none');
  });
});
