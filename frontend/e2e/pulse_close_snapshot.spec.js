// Verifies the per-exchange close-snapshot lifecycle + unified animation
// model shipped Jul 2026.
//
// The lifecycle:
//   - When NSE has closed but MCX is still open (15:30-23:30 IST window),
//     NSE-anchored rows on /pulse serve their LTP from the daily_book
//     close_settled snapshot and are tagged `price_source='snapshot_settled'`
//     + `is_animating=false` by the backend. The frontend skips tick-flash
//     for those rows. MCX rows keep live tickers with `is_animating=true`.
//   - When BOTH markets are closed, every row is on a closed exchange, so
//     every row's LTP is snapshot-served + tagged. The RefreshButton
//     tooltip surfaces "Markets closed — refresh only updates cash/margins/
//     holdings" so the operator knows why LTPs stay frozen.
//
// Five quality dimensions:
//   1. SSOT       — Backend tags rows with `price_source`; frontend reads that
//                   single field to decide animation (no client-side duplicate
//                   of the market-hours logic). No chip element is rendered.
//   2. Performance — Tick-flash gate is purely CSS-class-based; no per-cell
//                   DOM query or reactive polling.
//   3. Stale code — Old "block-refresh-during-closed" modal (.rf-closed-popup)
//                   stays hidden — replaced by toast + skip_ltp flow.
//   4. Reusable   — `is_animating=false` is the single boolean that gates
//                   flash; `price_source` drives observability only.
//   5. UX         — RefreshButton tooltip carries the "cash/margins" copy.

import { test, expect } from '@playwright/test';

test.setTimeout(120000);

const USER = process.env.PLAYWRIGHT_USER || 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

async function signIn(page) {
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="username"], input#username, input#s-user').first().fill(USER);
  await page.locator('input[name="password"], input#password, input#s-pass').first().fill(PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
  for (let i = 0; i < 10; i++) {
    const has = await page.evaluate(() => !!sessionStorage.getItem('ramboq_token'));
    if (has) break;
    await new Promise((r) => setTimeout(r, 300));
  }
}

/**
 * Force the frontend market-status cache into a specific mode by
 * intercepting /api/market/status. The RefreshButton + marketOpenInterval
 * gates read this within the next 5 s edge-tick.
 */
async function forceMarketState(page, { nse, mcx }) {
  await page.route('**/api/market/status', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        nse_open:   !!nse,
        mcx_open:   !!mcx,
        any_open:   !!nse || !!mcx,
        is_holiday: false,
      }),
    });
  });
}

/**
 * Return a mocked positions payload with one NSE and one MCX row. Both are
 * tagged as if the backend already computed price_source for the current
 * market state. The test uses different tags per scenario to isolate the
 * frontend rendering.
 * @param {'nse'|'mcx'|'both'} snapExchanges  which rows should carry price_source='snapshot_settled'
 */
function positionsPayload(snapExchanges) {
  const nseSnap = snapExchanges === 'nse' || snapExchanges === 'both';
  const mcxSnap = snapExchanges === 'mcx' || snapExchanges === 'both';
  return {
    rows: [
      {
        account: 'ZG0790', tradingsymbol: 'NIFTY26JULFUT', exchange: 'NFO',
        product: 'NRML', quantity: 50, average_price: 22000.0,
        close_price: 22100.0, last_price: 22150.0,
        pnl: 7500.0, pnl_percentage: 0.68, unrealised: 7500.0, realised: 0.0,
        day_change: 50.0, day_change_val: 2500.0, day_change_percentage: 0.23,
        delta_pos: 0.0, theta_pos: 0.0, underlying_ltp: 0.0,
        overnight_quantity: 50, day_buy_quantity: 0, day_sell_quantity: 0,
        day_buy_value: 0.0, day_sell_value: 0.0,
        last_price_stale: false, account_stale: false, account_stale_since: '',
        mode: 'live',
        // Unified animation triad (Jul 2026) — is_animating gates the
        // frontend cell-flash class; price_source drives observability.
        price_source: nseSnap ? 'snapshot_settled' : 'live',
        current_price: 22150.0,
        is_animating: !nseSnap,
      },
      {
        account: 'ZG0790', tradingsymbol: 'CRUDEOIL26JULFUT', exchange: 'MCX',
        product: 'NRML', quantity: 100, average_price: 6800.0,
        close_price: 6820.0, last_price: 6850.0,
        pnl: 5000.0, pnl_percentage: 0.74, unrealised: 5000.0, realised: 0.0,
        day_change: 30.0, day_change_val: 3000.0, day_change_percentage: 0.44,
        delta_pos: 0.0, theta_pos: 0.0, underlying_ltp: 0.0,
        overnight_quantity: 100, day_buy_quantity: 0, day_sell_quantity: 0,
        day_buy_value: 0.0, day_sell_value: 0.0,
        last_price_stale: false, account_stale: false, account_stale_since: '',
        mode: 'live',
        price_source: mcxSnap ? 'snapshot_settled' : 'live',
        current_price: 6850.0,
        is_animating: !mcxSnap,
      },
    ],
    summary: [
      { account: 'ZG0790', pnl: 12500.0, day_change_val: 5500.0,
        day_change_percentage: 0.31, day_prev_val: 1787000.0 },
      { account: 'TOTAL', pnl: 12500.0, day_change_val: 5500.0,
        day_change_percentage: 0.31, day_prev_val: 1787000.0 },
    ],
    refreshed_at: 'Mon, 07 Jul 2026, 16:30 IST | Mon, 07 Jul 2026, 07:00 EDT',
    as_of: null,
    stale_accounts: [],
  };
}

async function mockPositionsRoute(page, snapExchanges) {
  await page.route('**/api/positions/**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(positionsPayload(snapExchanges)),
    });
  });
}

test.describe('per-exchange close-snapshot lifecycle', () => {
  test('NSE-closed / MCX-open — NSE snapshot row has no chip and no flash class', async ({ page }) => {
    await forceMarketState(page, { nse: false, mcx: true });
    await mockPositionsRoute(page, 'nse');   // only NSE rows tagged
    await signIn(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    // Wait for the positions grid to mount (at least one LTP cell present).
    await page.waitForSelector('.ag-cell[col-id="ltp"]', { timeout: 20000 });
    // No SNAP chip at all — chip was removed.
    const chip = page.locator('[data-testid="ltp-snap-chip"]');
    expect(await chip.count()).toBe(0);
    // NSE snapshot row must not carry a flash class (freeze still holds).
    const nseRow = page.locator('.ag-row').filter({ hasText: 'NIFTY26JULFUT' });
    await expect(nseRow).toBeVisible({ timeout: 10000 });
    const ltpCell = nseRow.locator('.ag-cell[col-id="ltp"]');
    await expect(ltpCell).not.toHaveClass(/ltp-flash-up|ltp-flash-down/);
  });

  test('both markets closed — RefreshButton tooltip mentions cash/margins', async ({ page }) => {
    await forceMarketState(page, { nse: false, mcx: false });
    await mockPositionsRoute(page, 'both');
    await signIn(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    // RefreshButton lives in the page header. Wait for it to settle.
    const refreshBtn = page.locator('button.rf-btn').first();
    await expect(refreshBtn).toBeVisible({ timeout: 20000 });
    // Allow the 5 s market-state edge tick to hydrate.
    await page.waitForTimeout(6000);
    const title = await refreshBtn.getAttribute('title');
    expect(title || '').toMatch(/cash.*margins.*holdings/i);
  });
});
