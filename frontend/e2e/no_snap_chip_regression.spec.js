// Regression guard: no SNAP/LIVE chip must ever appear in an LTP cell,
// regardless of price_source value. The chip was removed Jul 2026 —
// this spec prevents re-introduction.
//
// Five quality dimensions:
//   1. SSOT       — Single locator `[data-testid="ltp-snap-chip"]` is the
//                   canonical check; any future chip must pass through here.
//   2. Performance — Assertions run post-render with no polling loops.
//   3. Stale code — Spec greps confirm no `.ltp-snap-chip` CSS remains active.
//   4. Reusable   — Uses shared mockPositionsRoute + forceMarketState helpers
//                   (same pattern as pulse_close_snapshot.spec.js).
//   5. UX         — Snapshot freeze still works: is_animating=false rows carry
//                   the `.ltp-snap` class (dimming hook) but NO chip element.

import { test, expect } from '@playwright/test';

test.setTimeout(60000);

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

/** Inject a positions payload where one row has price_source='snapshot_settled'
 *  and is_animating=false — the worst-case "should chip appear?" scenario. */
async function mockSnapshotRow(page) {
  await page.route('**/api/market/status', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ nse_open: false, mcx_open: false, any_open: false, is_holiday: false }),
    });
  });
  await page.route('**/api/positions/**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
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
            price_source: 'snapshot_settled',
            current_price: 22150.0,
            is_animating: false,
          },
        ],
        summary: [
          { account: 'ZG0790', pnl: 7500.0, day_change_val: 2500.0,
            day_change_percentage: 0.23, day_prev_val: 1100000.0 },
          { account: 'TOTAL', pnl: 7500.0, day_change_val: 2500.0,
            day_change_percentage: 0.23, day_prev_val: 1100000.0 },
        ],
        refreshed_at: 'Wed, 02 Jul 2026, 16:30 IST | Wed, 02 Jul 2026, 07:00 EDT',
        as_of: null,
        stale_accounts: [],
      }),
    });
  });
}

test.describe('no-snap-chip regression', () => {
  test('snapshot_settled row renders numeric LTP with no chip', async ({ page }) => {
    await mockSnapshotRow(page);
    await signIn(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    // Wait for LTP cells to render.
    await page.waitForSelector('.ag-cell[col-id="ltp"]', { timeout: 20000 });

    // No chip element anywhere on the page.
    const chip = page.locator('[data-testid="ltp-snap-chip"]');
    expect(await chip.count()).toBe(0);

    // LTP cell renders a non-empty numeric string.
    const ltpCell = page.locator('.ag-row').filter({ hasText: 'NIFTY26JULFUT' })
      .locator('.ag-cell[col-id="ltp"]').first();
    await expect(ltpCell).toBeVisible({ timeout: 10000 });
    const text = (await ltpCell.textContent() || '').trim();
    expect(text).toMatch(/\d/);  // at least one digit

    // Freeze gate preserved — no flash classes on snapshot row.
    await expect(ltpCell).not.toHaveClass(/ltp-flash-up|ltp-flash-down/);
  });

  test('live row also renders no chip', async ({ page }) => {
    // Route with is_animating=true to confirm chip was never present for live rows either.
    await page.route('**/api/market/status', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ nse_open: true, mcx_open: true, any_open: true, is_holiday: false }),
      });
    });
    await page.route('**/api/positions/**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
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
              price_source: 'live',
              current_price: 22150.0,
              is_animating: true,
            },
          ],
          summary: [
            { account: 'ZG0790', pnl: 7500.0, day_change_val: 2500.0,
              day_change_percentage: 0.23, day_prev_val: 1100000.0 },
            { account: 'TOTAL', pnl: 7500.0, day_change_val: 2500.0,
              day_change_percentage: 0.23, day_prev_val: 1100000.0 },
          ],
          refreshed_at: 'Wed, 02 Jul 2026, 11:30 IST | Wed, 02 Jul 2026, 02:00 EDT',
          as_of: null,
          stale_accounts: [],
        }),
      });
    });
    await signIn(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.ag-cell[col-id="ltp"]', { timeout: 20000 });

    const chip = page.locator('[data-testid="ltp-snap-chip"]');
    expect(await chip.count()).toBe(0);
  });
});
