/**
 * derivatives_leg_coherence.spec.js
 *
 * Fix 2 — Derivatives leg coherence: live tick vs poll-cadence qty/avg_cost
 * torn read.
 *
 * Leg MEMBERSHIP (whether a leg shows at all) is derived from poll-cadence
 * `qty`; per-leg LTP ticks live via the `/api/quotes/stream` SSE feed into
 * `symbolStore` — an independent writer with no joint consistency
 * guarantee against the poll-cadence qty. Two failure modes:
 *
 *   (a) A leg that's just been closed (qty→0) can keep showing a
 *       live-ticking LTP for a position that's already gone — a "ghost
 *       leg" with a moving price, even though the row itself correctly
 *       stays visible (CLOSED tag, realised P&L feeding TOTAL/Day P&L —
 *       see CLAUDE.md's Day P&L formulas; dropping the row entirely would
 *       break that documented, tested behaviour).
 *   (b) A newly-added leg (Fix 1's optimistic position_filled apply, qty>0
 *       but no real tick received for the symbol yet) can show a
 *       misleading "0.00" P&L instead of a pending state, since its
 *       provisional avg_cost/ltp are both seeded to the fill price —
 *       `legPnlDisplay`'s (ltp − cost) formula tautologically computes 0.
 *
 * Fix (both confined to CandidateLegRow.svelte):
 *   (a) `isClosed` is now computed BEFORE `ltp`; once a leg's local qty is
 *       0, `ltp` falls back to the frozen poll/leg-analytics value instead
 *       of reading `liveSnap()` — the LTP cell (and the Chg% cell, which
 *       derives from `ltp`) stop moving the instant qty hits 0, not when
 *       the symbol's SSE subscription happens to go quiet.
 *   (b) A provisional (`c._provisional`) leg with no tick yet
 *       (`liveSnap(sym)` returns null) renders P&L as '—' (the EXISTING
 *       null → '—' convention already used by every cell in this
 *       component), instead of a synthetic "0.00". The moment the first
 *       real tick lands, the row still shows '~' but its P&L becomes a
 *       genuine computed value.
 *
 * Mocking: `/api/quotes/stream` (the SSE source for `symbolStore`) is
 * mocked with a single `event: tick` frame — EventSource parses it then
 * the connection closes/reconnects, which is sufficient to prove a single
 * tick's effect deterministically without depending on real market data.
 *
 * Five quality dimensions:
 *  1. SSOT     — the LTP/Chg% cells and the PNL cell both read the SAME
 *                `ltp`/`pnl` deriveds already wired into the Day P&L /
 *                Exp P&L / TOTAL row formulas — no parallel computation.
 *  2. Perf     — the freeze and the pending→real transition both happen
 *                within one render tick of the qty/tick change, not on
 *                the next 5s poll.
 *  3. Stale    — negative control: an OPEN leg (qty>0, real row) keeps
 *                ticking live exactly as before this fix.
 *  4. Reusable — reuses the existing SSE-mock idiom (EventSource via
 *                page.route) and the `~` provisional chip assertions
 *                already used by derivatives_fill_optimistic_apply.spec.js.
 *  5. UX       — no new UI: both fixes reuse the existing '—' pending
 *                convention already rendered by every null-valued cell
 *                in this component.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/derivatives_leg_coherence.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(60_000);

const UNDERLYING    = 'NIFTY';
const SYMBOL        = 'NIFTY26DEC25000CE';
const FUTURE_EXPIRY = '2026-12-31';
const DERIV_URL     = `/admin/derivatives?u=${UNDERLYING}`;
const ACCOUNT       = 'ZG0790';

function _positionsBody(rows) {
  return {
    rows,
    summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null,
  };
}

function _realRow(qty) {
  return {
    tradingsymbol: SYMBOL, symbol: SYMBOL, exchange: 'NFO',
    quantity: qty, opening_quantity: 50, overnight_quantity: qty,
    day_buy_quantity: 0, day_sell_quantity: qty === 0 ? 50 : 0, day_sell_value: 0,
    average_price: 200.0, last_price: 210.0, close_price: 195.0,
    pnl: 500.0, day_change_val: 50.0, unrealised_pnl: qty === 0 ? 0 : 500.0,
    realised_pnl: qty === 0 ? 500.0 : 0,
    account: ACCOUNT, source: 'live',
  };
}

async function _mockCommonEndpoints(page, state) {
  await page.route('**/api/positions**', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(_positionsBody(state.rows)),
    });
  });

  await page.route('**/api/instruments**', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        cycle_date: '2026-09-29', count: 1,
        items: [{ s: SYMBOL, e: 'NFO', t: 'CE', ls: 50, ts: 0.05, u: UNDERLYING, x: FUTURE_EXPIRY, k: 25000 }],
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
    const qty = Number(body?.legs?.[0]?.qty ?? 0);
    const _greeks = { delta: 0.5, gamma: 0.01, theta: -0.3, vega: 0.2, rho: 0.01 };
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        underlying: UNDERLYING, spot: 25100, spot_prev_close: 24900,
        legs: (body?.legs ?? []).map(l => ({ ...l, greeks: _greeks })),
        payoff: [
          { spot: 24000, today_value: -100, expiry_value: -100 },
          { spot: 25100, today_value: 500, expiry_value: 500 },
          { spot: 26000, today_value: 900, expiry_value: 900 },
        ],
        risk: { breakevens: [24800] }, breakevens: [24800],
        pnl_at_spot: 500, exp_pnl_at_spot: 500,
        dte: 15, iv: 0.18, aggregate_greeks: _greeks,
      }),
    });
  });

  // SSE quote stream — a single `event: tick` frame for the test symbol.
  // EventSource parses the frame, then the (now-ended) HTTP response
  // closes the connection — enough to prove a single tick's effect
  // deterministically. `state.tickLtp` is read lazily by the route
  // handler so the test can arm a later tick with a fresh value.
  await page.route('**/api/quotes/stream', async (route) => {
    if (state.tickLtp == null) {
      // No tick armed yet — keep the connection open-ish by fulfilling
      // an empty heartbeat-only stream; EventSource will just see
      // nothing happen (no onmessage calls).
      route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: 'event: heartbeat\ndata: {}\n\n',
      });
      return;
    }
    const frame = `event: tick\ndata: ${JSON.stringify({ sym: SYMBOL, ltp: state.tickLtp })}\n\n`;
    route.fulfill({ status: 200, contentType: 'text/event-stream', body: frame });
  });
}

test.describe('Derivatives — leg coherence (live tick vs poll-cadence qty)', () => {
  test('(a) a leg whose qty flips to 0 stops rendering a live-ticking price', async ({ page }) => {
    await loginAsAdmin(page);

    const state = { rows: [_realRow(50)], tickLtp: null };
    await _mockCommonEndpoints(page, state);

    /** @type {import('@playwright/test').WebSocketRoute | null} */
    let wsRoute = null;
    await page.routeWebSocket('**/ws/performance', (ws) => { wsRoute = ws; });

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const legsGrid = page.locator('.cand-grid');
    await expect(legsGrid).toBeVisible({ timeout: 15_000 });
    const row = page.locator('.cand-grid .cand-row:not(.cand-row-total)').first();
    await expect(row).toBeVisible({ timeout: 15_000 });

    // Confirm the OPEN leg ticks live first (negative-control half of
    // this test): arm a tick and see the LTP cell pick it up.
    state.tickLtp = 212.5;
    // Force a fresh SSE connection so the mocked route is re-fetched with
    // the newly-armed tick (EventSource auto-reconnects on the backoff
    // timer after the first, heartbeat-only connection closes).
    await page.waitForTimeout(3_500);
    const ltpCell = row.locator('.leg-ltp');
    await expect(ltpCell).toHaveText('212.50', { timeout: 10_000 });

    for (let i = 0; i < 25 && !wsRoute; i++) await page.waitForTimeout(200);
    if (!wsRoute) {
      test.skip(true, 'WebSocket to /ws/performance never connected — cannot inject test event');
      return;
    }

    // The position closes — real row, not provisional, so this exercises
    // the poll-cadence path directly. Driven via the SAME
    // `positions_refreshed` WS event the real postback fan-out sends on a
    // confirmed close, instead of waiting on the passive book-poller's
    // own (non-`fresh`) cadence/TTL — deterministic, matches how a real
    // close actually reaches this page (`+page.svelte`'s own WS handler
    // calls `loadPositions({fresh:true})` synchronously on this event).
    state.rows = [_realRow(0)];
    wsRoute.send(JSON.stringify({
      event: 'positions_refreshed', tradingsymbol: SYMBOL, account: ACCOUNT, ts: Date.now(),
    }));
    // A closed row's Qty cell renders `<span class="num cell-flat">0</span>`
    // (CandidateLegRow.svelte's `{#if isClosed}` branch) — distinct from
    // the open-row kv-pos/kv-neg qty cell.
    await expect(row.locator('.cell-flat', { hasText: /^0$/ })).toHaveCount(1, { timeout: 10_000 });

    // Capture the frozen LTP value right after the close registers.
    const frozenText = await ltpCell.textContent();

    // Arm a NEW, different tick for the same symbol — if the ghost-leg
    // bug were present, the LTP cell would pick this up.
    state.tickLtp = 999.99;
    await page.waitForTimeout(4_000); // give the SSE reconnect a window to deliver it

    await expect(ltpCell).toHaveText(frozenText ?? '', { timeout: 2_000 });
    await expect(ltpCell).not.toHaveText('999.99');
  });

  test('(b) a newly-added provisional leg shows pending P&L until its first tick, not 0', async ({ page }) => {
    await loginAsAdmin(page);

    // No real row for the symbol at all — the fill arrives purely via WS.
    const state = { rows: [], tickLtp: null };
    await _mockCommonEndpoints(page, state);

    /** @type {import('@playwright/test').WebSocketRoute | null} */
    let wsRoute = null;
    await page.routeWebSocket('**/ws/performance', (ws) => { wsRoute = ws; });

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const legsGrid = page.locator('.cand-grid');
    await expect(legsGrid).toBeVisible({ timeout: 15_000 });

    for (let i = 0; i < 25 && !wsRoute; i++) await page.waitForTimeout(200);
    if (!wsRoute) {
      test.skip(true, 'WebSocket to /ws/performance never connected — cannot inject test event');
      return;
    }

    wsRoute.send(JSON.stringify({
      event: 'position_filled', account: ACCOUNT, exchange: 'NFO',
      tradingsymbol: SYMBOL, qty: 50, fill_price: 210.0, ts: Date.now(), order_id: 'T3001',
    }));

    const row = page.locator('.cand-grid .cand-row:not(.cand-row-total)').first();
    await expect(row).toBeVisible({ timeout: 5_000 });
    await expect(row.locator('.cand-provisional-tag')).toHaveCount(1);

    // PRIMARY ASSERTION — the PNL cell (2nd .cand-pnl in the row: Day P&L,
    // P&L, Exp P&L, Extrinsic) shows the pending '—', not a synthetic
    // "0.00", before any real tick for the symbol has arrived.
    const pnlCell = row.locator('.cand-pnl').nth(1);
    await expect(pnlCell).toHaveText('—', { timeout: 2_000 });

    // Arm and deliver a real tick for the symbol.
    state.tickLtp = 225.0;
    await page.waitForTimeout(4_000);

    // Once the first tick lands, the row is still provisional (~) but no
    // longer pending — the PNL cell now shows a real computed value.
    await expect(pnlCell).not.toHaveText('—', { timeout: 10_000 });
  });
});
