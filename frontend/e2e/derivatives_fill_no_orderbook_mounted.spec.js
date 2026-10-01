/**
 * derivatives_fill_no_orderbook_mounted.spec.js
 *
 * Operator-reported bug: on /admin/derivatives, an order fill does not
 * update the Legs grid / Payoff chart until the operator presses the
 * manual refresh button.
 *
 * This follows TWO already-shipped fixes for the same symptom:
 *   - c90a9d04 — fixed the Payoff chart's own refetch-trigger effect to
 *     re-fire `loadStrategy()` on any legs-signature change.
 *   - 0b862409 — added `noteOrderPollFills()` (orderFillDetector.js),
 *     wired into OrderBook.svelte's and LogPanel.svelte's own
 *     `_loadOrders()` poll loops, as a backstop for fills that never
 *     broadcast a WS event (broker-order-book TTL read, 5-min
 *     open_order_watchdog sweep, admin reconcile).
 *
 * CONFIRMED ROOT CAUSE of the still-reported bug: `noteOrderPollFills()`
 * is called ONLY from inside OrderBook.svelte's / LogPanel.svelte's own
 * poll loops. `/admin/derivatives` mounts NEITHER component unless the
 * operator opens the embedded ticket (clicking a candidate row mounts
 * SymbolPanel's own OrderBook card) — see
 * order_fill_plain_poll_refresh.spec.js, whose setup has to click a
 * candidate row just to mount an OrderBook before the 0b862409 fix can
 * be observed at all. Sitting on the Derivatives page with nothing else
 * open, a fill delivered through one of the three silent channels never
 * reaches the detector, and the page is left depending entirely on a
 * genuine broker-postback WS event (`position_filled` / `order_update`)
 * — which the derivatives page DOES handle directly, but which never
 * fires for the three silent channels, or for brokers whose postback
 * delivery is unreliable (Dhan/Groww, per CLAUDE.md).
 *
 * Fix: `orderFillPoller.js`'s `pollOrderFillWatch()`, started once from
 * the (algo) layout root (same layout-resident-singleton pattern as
 * `startBookPollers()`), polls the same `fetchOrders()` +
 * `fetchAlgoOrdersRecent()` pair OrderBook/LogPanel already use and
 * feeds the merged rows into the SAME shared `noteOrderPollFills()`
 * detector — now running regardless of which page/components are
 * mounted.
 *
 * Mock design: identical "stale-unless-fresh" idiom as
 * order_fill_plain_poll_refresh.spec.js — `/api/positions**` only
 * returns the new quantity to a `fresh=1` request, so a fresh
 * strategy-analytics POST carrying the new quantity can ONLY have been
 * produced by the detector bumping `bookChanged` → `loadPositions({
 * fresh: true })`, not by the pre-existing non-fresh 5s global
 * book-poller.
 *
 * Five quality dimensions:
 *  1. SSOT  — Legs grid and Payoff chart (strategy-analytics POST body)
 *             agree on the post-fill quantity from the same fresh fetch.
 *  2. Perf  — refresh lands within one layout-poller tick (5s) of the
 *             order log showing COMPLETE, not "eventually".
 *  3. Stale — negative control: an order already COMPLETE on the very
 *             first poll must not trigger a spurious fresh refetch.
 *  4. Reuse — reuses the exact mocking idiom from
 *             order_fill_plain_poll_refresh.spec.js /
 *             derivatives_fill_refresh.spec.js.
 *  5. UX    — explicitly asserts "Order Book" is NEVER rendered/mounted
 *             anywhere on the page for the duration of the test, so the
 *             fix cannot be accidentally validated by some other
 *             component's poller.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/derivatives_fill_no_orderbook_mounted.spec.js \
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

/** Same IST-wall-clock-stamping concern as order_fill_plain_poll_refresh.spec.js
 *  — not actually load-bearing here (OrderBook's session filter never runs
 *  since OrderBook never mounts in this spec), kept for parity/clarity. */
function _nowIstTimestamp() {
  const shifted = new Date(Date.now() + 5.5 * 60 * 60 * 1000);
  return shifted.toISOString().slice(0, 19).replace('T', ' ');
}

function _ordersBody(status) {
  return {
    rows: [
      {
        order_id: 'T9101', tradingsymbol: SYMBOL, exchange: 'NFO',
        transaction_type: 'BUY', quantity: 5, price: 455.0, average_price: 455.0,
        status, account: ACCOUNT,
        order_timestamp: _nowIstTimestamp(),
      },
    ],
  };
}

async function _mockCommonEndpoints(page, state) {
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

  // The plain-poll path under test — identical endpoints the layout's
  // new orderFillPoller.js hits (same fetchOrders()/fetchAlgoOrdersRecent()
  // pair OrderBook/LogPanel use), but NO component on this page ever
  // mounts to poll them itself.
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
  // Layout's chase chip poller — independent of the fix under test, but
  // must not error/hang the layout.
  await page.route('**/api/orders/events/recent**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) });
  });
}

test.describe('Derivatives page with NO OrderBook/LogPanel mounted anywhere', () => {
  test('a fill detected ONLY via the layout-level poll (no WS event, no OrderBook/LogPanel mounted) still forces a fresh books/strategy refetch', async ({ page }) => {
    await loginAsAdmin(page);

    const state = { qty: 5, cachedQty: 5, orderStatus: 'OPEN', strategyCalls: [] };
    await _mockCommonEndpoints(page, state);

    // Connect the WS route but NEVER send anything over it — proves the
    // refresh below is not secretly WS-driven.
    await page.routeWebSocket('**/ws/performance', () => {});

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const legsGrid = page.locator('.cand-grid');
    await expect(legsGrid).toBeVisible({ timeout: 15_000 });
    const qty5Cell = page.locator('.cand-grid .cand-row:not(.cand-row-total) .kv-pos', { hasText: /^5$/ });
    await expect(qty5Cell).toHaveCount(1, { timeout: 15_000 });
    await expect.poll(() => state.strategyCalls.some(c => c.qty === 5), { timeout: 15_000 }).toBe(true);

    // Confirm the gap: NEVER click the candidate row, never open the
    // ticket/SymbolPanel — so OrderBook.svelte's own card never mounts.
    // "Order Book" must have zero matches on screen for the whole test.
    await expect(page.getByText('Order Book', { exact: false })).toHaveCount(0);

    // Let a few seconds pass while the order is still OPEN — this is the
    // detector's seeding pass; it must not fire anything.
    await page.waitForTimeout(3_000);
    await expect(page.getByText('Order Book', { exact: false })).toHaveCount(0);
    const callsBeforeFill = state.strategyCalls.length;

    // Simulate the fill arriving ONLY via the plain-poll backstop: the
    // broker's own order book now shows COMPLETE, and the genuinely
    // fresh position quantity becomes available — but the (unbusted)
    // positions cache keeps serving the OLD quantity to any non-fresh
    // caller. No WS message is sent at any point in this test.
    state.orderStatus = 'COMPLETE';
    state.qty = 10;
    const _fillVisibleAt = Date.now();

    // PRIMARY ASSERTION — a fresh strategy-analytics POST carrying the
    // NEW quantity (10) lands within a couple of layout-poller ticks
    // (5s cadence) of the order log showing COMPLETE, with NOTHING else
    // on the page capable of producing it (no OrderBook, no LogPanel, no
    // WS event).
    await expect.poll(
      () => state.strategyCalls.some(c => c.qty === 10 && c.ts - _fillVisibleAt < 12_000),
      { timeout: 15_000 },
    ).toBe(true);

    console.log('[derivatives_fill_no_orderbook_mounted] strategyCalls:', JSON.stringify(state.strategyCalls));
    expect(state.strategyCalls.length).toBeGreaterThan(callsBeforeFill);

    // Legs grid itself must also settle on the fresh quantity.
    const qty10Cell = page.locator('.cand-grid .cand-row:not(.cand-row-total) .kv-pos', { hasText: /^10$/ });
    await expect(qty10Cell).toHaveCount(1, { timeout: 5_000 });

    // Re-confirm OrderBook never mounted during the whole sequence.
    await expect(page.getByText('Order Book', { exact: false })).toHaveCount(0);
  });

  test('negative control: an order already COMPLETE on the very first poll does not trigger a spurious fresh refetch', async ({ page }) => {
    await loginAsAdmin(page);

    // Order is ALREADY filled before the page ever mounts — simulates
    // opening the derivatives page well after a fill happened. Must be
    // treated as pre-existing state (seeding pass), not a "just
    // happened" transition.
    const state = { qty: 5, cachedQty: 5, orderStatus: 'COMPLETE', strategyCalls: [] };
    await _mockCommonEndpoints(page, state);

    await page.routeWebSocket('**/ws/performance', () => {});

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const legsGrid = page.locator('.cand-grid');
    await expect(legsGrid).toBeVisible({ timeout: 15_000 });
    await expect.poll(() => state.strategyCalls.some(c => c.qty === 5), { timeout: 15_000 }).toBe(true);
    await expect(page.getByText('Order Book', { exact: false })).toHaveCount(0);

    // Let several poller ticks pass with the order staying COMPLETE
    // throughout (steady state, no transition) — no fresh=1 call, and
    // therefore no qty=10 strategy-analytics POST, should ever appear.
    await page.waitForTimeout(12_000);

    const sawFreshQty = state.strategyCalls.some(c => c.qty === 10);
    expect(sawFreshQty).toBe(false);
  });
});
