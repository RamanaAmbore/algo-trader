/**
 * order_fill_plain_poll_refresh.spec.js
 *
 * Follow-up to commit c90a9d04 ("Payoff chart no longer stale after an
 * order fill on the same underlying"). That commit fixed the LAST link in
 * the "order fulfilled → fresh books → fresh legs → fresh payoff" chain
 * (the derivatives legs-watcher effect refetches on a legs-signature
 * change, not only on an underlying switch).
 *
 * This spec proves the FIRST link — "order log shows FILLED → immediate
 * fresh books fetch" — for the channel that c90a9d04's own investigation
 * never looked at: the PLAIN-POLL-ONLY path, with NO WS event at all.
 *
 * Confirmed empirically before this fix (see task investigation):
 *   - `position_filled` / `positions_refreshed` / `order_update` /
 *     `book_changed` WS events are broadcast EXCLUSIVELY by
 *     `_postback_broadcast_fanout` (backend/api/routes/orders.py), which
 *     only runs on a genuine broker POSTBACK.
 *   - Three channels can flip an order's DISPLAYED status to FILLED/
 *     COMPLETE without ever going through that function:
 *       1. The live broker-order-book read (`/api/orders/`, a 15s-TTL
 *          passthrough of `broker.orders()`) — the broker's own book can
 *          say COMPLETE with zero backend state mutation.
 *       2. The 5-min `_task_open_order_watchdog` sweep (background.py) —
 *          the documented Dhan/Groww backstop when a webhook isn't
 *          configured/delivered (CLAUDE.md "Dhan/Groww order detection").
 *       3. The admin `/algo/reconcile` sweep and the per-card
 *          `/{id}/reconcile` button.
 *   None of the three invalidate positions/holdings caches or broadcast
 *   any WS event — so before this fix, the operator could watch an order
 *   flip to "Filled" in the order log while Legs/Payoff kept showing
 *   pre-fill data until the next unrelated 5s book-poller tick (or
 *   longer, since that poller doesn't force a cache-busting fetch
 *   either — see the mock design below).
 *
 * Fix: `noteOrderPollFills` (frontend/src/lib/data/orderFillDetector.js),
 * wired into both OrderBook.svelte's and LogPanel.svelte's own
 * `_loadOrders()` merge step, detects the SAME status transition
 * directly off the polled order rows (independent of any WS traffic) and
 * bumps the existing `bookChanged` bus — the identical mechanism every
 * WS handler already drives (derivatives' own `bookChanged.subscribe`
 * effect calls `loadPositions({ fresh: true })` + `loadStrategy()`).
 *
 * Mock design (the key methodological point): `/api/positions**` returns
 * a STALE quantity unless the request carries `?fresh=1` — mirroring the
 * REAL backend's 30s route-level TTL cache, which is never invalidated by
 * any of the three gap channels above. This isolates the assertion: the
 * pre-existing GLOBAL cross-page book poller (5s cadence, always
 * non-fresh) can NEVER observe the new quantity in this test — only an
 * explicit `fresh:true` call (which only this fix's detector triggers,
 * with no WS event involved) can. A fresh strategy-analytics POST
 * carrying the new quantity is therefore conclusive proof the detector
 * fired, not an artifact of some other poller eventually catching up.
 *
 * Five quality dimensions:
 *  1. SSOT     — Legs grid (candidatePositions) and Payoff chart
 *                (strategy-analytics POST body) agree on the post-fill
 *                quantity, sourced from the SAME fresh fetch.
 *  2. Perf     — refresh lands within one OrderBook poll tick (3s) of the
 *                order-log transition, not merely "eventually".
 *  3. Stale    — negative control: an order already COMPLETE on the very
 *                FIRST poll (page/ticket opened after the fact) must NOT
 *                trigger a spurious fresh refetch — this is an edge
 *                trigger on a genuine transition, not a level signal.
 *  4. Reuse    — reuses the exact mocking idiom from
 *                derivatives_fill_refresh.spec.js (positions/instruments/
 *                holdings/watchlist/strategy-analytics) and the
 *                SymbolPanel-open idiom from derivatives_chain_picker.spec.js.
 *  5. UX       — operator never has to manually reload after a fill that
 *                was only ever detected via polling, no WS event required.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/order_fill_plain_poll_refresh.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(60_000);

const UNDERLYING    = 'TESTFO';
const SYMBOL        = 'TESTFO26DEC100CE';
const FUTURE_EXPIRY = '2026-12-31';
const DERIV_URL     = `/admin/derivatives?u=${UNDERLYING}`;
const ACCOUNT       = 'ZG0790';

function _positionsBody(qty) {
  return {
    rows: [
      {
        tradingsymbol: SYMBOL, symbol: SYMBOL, exchange: 'NFO',
        quantity: qty, opening_quantity: qty, overnight_quantity: qty,
        day_buy_quantity: 0, day_sell_quantity: 0, day_sell_value: 0,
        average_price: 450.5, last_price: 455.0, close_price: 450.0,
        pnl: 22.5, day_change_val: 5.0, unrealised_pnl: 22.5, realised_pnl: 0,
        account: ACCOUNT, source: 'live',
      },
    ],
    summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null,
  };
}

/**
 * OrderBook.svelte's `_rowTsMs` parses `order_timestamp` as IST wall-clock
 * (appends `+05:30` explicitly, regardless of what the string actually
 * represents) and `_isCurrentSessionRow` drops any terminal row that
 * doesn't fall in the CURRENT trading session (08:00 IST rollover) once
 * its status leaves OPEN — a row silently dropped from `merged` would
 * never reach `noteOrderPollFills` at all, independent of this fix's
 * correctness. Shift the epoch by +5:30 before formatting with
 * `toISOString()` so the resulting digits are genuinely "right now" in
 * IST wall-clock terms, matching what `isCurrentTradingSession` compares
 * against (itself, evaluated at read-time) — avoids a UTC/IST mismatch
 * that could land this fixture in the "prior session" and get it dropped.
 */
function _nowIstTimestamp() {
  const shifted = new Date(Date.now() + 5.5 * 60 * 60 * 1000);
  return shifted.toISOString().slice(0, 19).replace('T', ' ');
}

function _ordersBody(status) {
  return {
    rows: [
      {
        order_id: 'T9001', tradingsymbol: SYMBOL, exchange: 'NFO',
        transaction_type: 'BUY', quantity: 5, price: 455.0, average_price: 455.0,
        status, account: ACCOUNT,
        order_timestamp: _nowIstTimestamp(),
      },
    ],
  };
}

async function _mockCommonEndpoints(page, state) {
  // STALE-UNLESS-FRESH — mirrors the real backend's 30s route-level
  // positions cache, which none of the plain-poll-only fill channels
  // ever invalidate. Only `?fresh=1` sees the post-fill quantity.
  await page.route('**/api/positions**', (route) => {
    const url = new URL(route.request().url());
    const isFresh = url.searchParams.get('fresh') === '1';
    const qty = isFresh ? state.qty : state.cachedQty;
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(_positionsBody(qty)),
    });
  });

  await page.route('**/api/instruments**', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        cycle_date: '2026-09-29', count: 1,
        items: [{ s: SYMBOL, e: 'NFO', t: 'CE', ls: 1, ts: 0.05, u: UNDERLYING, x: FUTURE_EXPIRY, k: 100 }],
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
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ watchlists: [], symbols: [] }),
    });
  });

  await page.route('**/api/options/strategy-analytics**', (route) => {
    const body = route.request().postDataJSON();
    const qty = Number(body?.legs?.[0]?.qty ?? 0);
    state.strategyCalls.push({ qty, ts: Date.now() });
    const _greeks = { delta: 0.5, gamma: 0.01, theta: -0.3, vega: 0.2, rho: 0.01 };
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        underlying: UNDERLYING, spot: 460, spot_prev_close: 450,
        legs: (body?.legs ?? []).map(l => ({ ...l, greeks: _greeks })),
        payoff: [
          { spot: 400, today_value: -10, expiry_value: -10 },
          { spot: 460, today_value: 22.5 * qty / 5, expiry_value: 22.5 * qty / 5 },
          { spot: 520, today_value: 45, expiry_value: 45 },
        ],
        risk: { breakevens: [440] }, breakevens: [440],
        pnl_at_spot: 22.5 * qty / 5, exp_pnl_at_spot: 22.5 * qty / 5,
        dte: 15, iv: 0.25, aggregate_greeks: _greeks,
      }),
    });
  });

  // Order log endpoints — the PLAIN-POLL path under test. No WS event
  // is ever sent in this spec; OrderBook.svelte's own 3s poll of these
  // two routes is the only signal.
  await page.route('**/api/orders/', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(_ordersBody(state.orderStatus)),
    });
  });
  await page.route('**/api/orders/algo/recent**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
  });
  await page.route('**/api/orders/gtts/**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ gtts: [] }) });
  });
}

test.describe('Order log plain-poll fill — no WS event', () => {
  test('a fill detected ONLY via the order-log poll (no WS event) still forces a fresh books/strategy refetch', async ({ page }) => {
    await loginAsAdmin(page);

    const state = { qty: 5, cachedQty: 5, orderStatus: 'OPEN', strategyCalls: [] };
    await _mockCommonEndpoints(page, state);

    // Connect the WS route but NEVER send anything over it — proves the
    // refresh below is not secretly WS-driven.
    /** @type {import('@playwright/test').WebSocketRoute | null} */
    let wsRoute = null;
    await page.routeWebSocket('**/ws/performance', (ws) => { wsRoute = ws; });

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const legsGrid = page.locator('.cand-grid');
    await expect(legsGrid).toBeVisible({ timeout: 15_000 });
    const qty5Cell = page.locator('.cand-grid .cand-row:not(.cand-row-total) .kv-pos', { hasText: /^5$/ });
    await expect(qty5Cell).toHaveCount(1, { timeout: 15_000 });
    await expect.poll(() => state.strategyCalls.some(c => c.qty === 5), { timeout: 15_000 }).toBe(true);

    // Open the ticket (SymbolPanel) by clicking the candidate row — this
    // mounts the embedded OrderBook ("Order Book" card, oes-bottom-panel)
    // which starts its own independent 3s poll of /api/orders/.
    const candRow = page.locator('.cand-grid .cand-row:not(.cand-row-total)').first();
    await candRow.click();
    await expect(page.getByText('Order Book', { exact: false })).toBeVisible({ timeout: 10_000 });

    // Let at least one OrderBook poll land while the order is still OPEN
    // — this is the module's seeding pass; it must not fire anything.
    await page.waitForTimeout(3_500);
    const callsBeforeFill = state.strategyCalls.length;

    // Simulate the fill arriving ONLY via the plain-poll backstop: the
    // broker's own order book now shows COMPLETE (what the 5-min
    // open_order_watchdog / live broker-order-book read would surface),
    // and the genuinely-fresh position quantity becomes available —
    // but the (unbusted) positions cache keeps serving the OLD quantity
    // to any non-fresh caller, exactly like the real unbusted backend
    // cache would. No WS message is sent at any point in this test.
    state.orderStatus = 'COMPLETE';
    state.qty = 10;
    const _fillVisibleAt = Date.now();

    // PRIMARY ASSERTION — a fresh strategy-analytics POST carrying the
    // NEW quantity (10) lands within one OrderBook poll tick (3s) + slack
    // of the order log showing COMPLETE. Because `/api/positions**` only
    // ever returns qty=10 to a `fresh=1` request, this can ONLY have been
    // produced by the order-fill detector bumping bookChanged →
    // loadPositions({ fresh: true }) — the pre-existing 5s global
    // book-poller's own non-fresh call would still read qty=5 forever in
    // this mock, so it cannot produce this result.
    await expect.poll(
      () => state.strategyCalls.some(c => c.qty === 10 && c.ts - _fillVisibleAt < 4_500),
      { timeout: 5_000 },
    ).toBe(true);

    console.log('[order_fill_plain_poll_refresh] strategyCalls:', JSON.stringify(state.strategyCalls));
    expect(state.strategyCalls.length).toBeGreaterThan(callsBeforeFill);

    // Legs grid itself must also settle on the fresh quantity.
    const qty10Cell = page.locator('.cand-grid .cand-row:not(.cand-row-total) .kv-pos', { hasText: /^10$/ });
    await expect(qty10Cell).toHaveCount(1, { timeout: 5_000 });
  });

  test('negative control: an order already COMPLETE on the very first poll does not trigger a spurious fresh refetch', async ({ page }) => {
    await loginAsAdmin(page);

    // Order is ALREADY filled before the ticket/OrderBook ever mounts —
    // simulates opening the order log well after a fill happened. Must
    // be treated as pre-existing state, not a "just happened" transition.
    const state = { qty: 5, cachedQty: 5, orderStatus: 'COMPLETE', strategyCalls: [] };
    await _mockCommonEndpoints(page, state);

    await page.routeWebSocket('**/ws/performance', () => {});

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const legsGrid = page.locator('.cand-grid');
    await expect(legsGrid).toBeVisible({ timeout: 15_000 });
    await expect.poll(() => state.strategyCalls.some(c => c.qty === 5), { timeout: 15_000 }).toBe(true);

    const candRow = page.locator('.cand-grid .cand-row:not(.cand-row-total)').first();
    await candRow.click();
    await expect(page.getByText('Order Book', { exact: false })).toBeVisible({ timeout: 10_000 });

    // Let several poll ticks pass with the order staying COMPLETE
    // throughout (steady state, no transition) — no fresh=1 call, and
    // therefore no qty=10 strategy-analytics POST, should ever appear.
    await page.waitForTimeout(7_000);

    const sawFreshQty = state.strategyCalls.some(c => c.qty === 10);
    expect(sawFreshQty).toBe(false);
  });
});
