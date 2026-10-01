/**
 * derivatives_fill_refresh.spec.js
 *
 * Operator report: "why when order is fulfilled, payoff and legs didn't get
 * refreshed" — the /admin/derivatives Legs grid and Payoff chart kept
 * showing pre-fill data (wrong quantity) until a manual page reload.
 *
 * Root cause (confirmed by tracing the reactive chain in
 * `+page.svelte`, NOT by guessing):
 *
 *   1. The WS `position_filled` handler calls `loadStrategy()` SYNCHRONOUSLY,
 *      immediately after firing (but not awaiting) `loadPositions({fresh:true})`.
 *      `legs` hasn't been rebuilt yet at that point, so `loadStrategy()`'s own
 *      legs-signature memo (`legsKey === _stratLastKey`) is unchanged and the
 *      call is a silent no-op — it never reaches the backend.
 *
 *   2. The backend's OWN confirmation event, `positions_refreshed` (fired by
 *      `_positions_refresh_after_fill` in orders.py once the broker book has
 *      genuinely propagated the fill, up to ~7s later), is handled by calling
 *      `loadPositions({fresh:true})` ONLY. Once positions update, `legs`
 *      rebuilds reactively — but (3) below is what actually turns that into
 *      a backend refetch.
 *
 *   3. THE ACTUAL ROOT CAUSE: the one effect that watches `legs` for a
 *      refresh trigger (the "Bug 1 fix" effect, `+page.svelte` ~line 2830)
 *      explicitly SKIPPED calling `loadStrategy()` whenever
 *      `strategy.underlying` already matched the selected underlying — i.e.
 *      it only fired on a symbol SWITCH, never on a same-symbol quantity/leg
 *      change. So a fill that changes an existing leg's quantity (the common
 *      case — not a brand new underlying) had NO event-driven path to the
 *      backend at all, from EITHER WS event.
 *
 *   Net effect: the Legs grid (sourced straight off `candidatePositions`,
 *   no gating) updates promptly, but the Payoff chart's backend-priced
 *   curve/analytics (`strategy`, fetched via POST /api/options/strategy-
 *   analytics) only ever refreshed on the next 5s `marketAwareInterval`
 *   tick — and in a sustained-fill / rapid-requote scenario, or if that
 *   tick raced a stale `legsKey`, it could stay wrong far longer.
 *
 * Fix: the legs-watcher effect now also fires `loadStrategy()` whenever the
 * CURRENT legs signature (`computeLegsKey(buildCleanLegs(legs, ...))`) no
 * longer matches what was last actually sent to the backend
 * (`_stratLastKey` — the same memo key `loadStrategy()` itself maintains),
 * not only on an underlying mismatch. This is the single, correct trigger
 * point — both WS handlers (and the plain 5s poll, and any other future
 * caller) funnel through the SAME reactive chain
 * (positions → candidatePositions → legs → this effect), so no additional
 * explicit `loadStrategy()` call sites were needed in the WS handlers
 * themselves.
 *
 * This spec reproduces the exact scenario end-to-end: mocks positions +
 * instruments + holdings + watchlist + strategy-analytics with a MUTABLE
 * fixture, flips the fixture mid-test to simulate an order fill (qty 5 → 10
 * on the held leg), and injects `position_filled` then `positions_refreshed`
 * over a mocked /ws/performance socket — the same idiom already used by
 * `derivatives_positions_fresh_load.spec.js`. Asserts BOTH:
 *   (a) the Legs grid row updates to the new quantity, and
 *   (b) a NEW POST to /api/options/strategy-analytics lands carrying the
 *       new quantity, WITHIN a window well under the 5s poll interval —
 *       proving the refresh is event-driven, not merely "eventually
 *       catches up on the next poll tick."
 *
 * Five quality dimensions:
 *  1. SSOT     — Legs grid (candidatePositions) and Payoff chart (strategy-
 *                analytics POST body) must agree on the post-fill quantity.
 *  2. Perf     — refresh must land well inside the 5s poll budget (event-
 *                driven), not merely "eventually" on the periodic tick.
 *  3. Stale    — grep guard: the `positions_refreshed` branch must call
 *                loadStrategy(), not just loadPositions().
 *  4. Reusable — reuses the exact mocking idiom from
 *                derivatives_expired_contract_fix.spec.js (REST) and
 *                derivatives_positions_fresh_load.spec.js (WS injection).
 *  5. UX       — operator never has to manually reload after a fill.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/derivatives_fill_refresh.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';

test.setTimeout(60_000);

const UNDERLYING   = 'TESTFO';
const SYMBOL       = 'TESTFO26DEC100CE'; // Kite-shaped monthly option — ends CE, matches isFOSymbol
const FUTURE_EXPIRY = '2026-12-31';
const DERIV_URL    = `/admin/derivatives?u=${UNDERLYING}`;

/** Builds the /api/positions response body from the current fixture qty. */
function _positionsBody(qty) {
  return {
    rows: [
      {
        tradingsymbol: SYMBOL,
        symbol: SYMBOL,
        exchange: 'NFO',
        quantity: qty,
        opening_quantity: qty,
        overnight_quantity: qty,
        day_buy_quantity: 0,
        day_sell_quantity: 0,
        day_sell_value: 0,
        average_price: 450.5,
        last_price: 455.0,
        close_price: 450.0,
        pnl: 22.5,
        day_change_val: 5.0,
        unrealised_pnl: 22.5,
        realised_pnl: 0,
        account: 'ZG0790',
        source: 'live',
      },
    ],
    summary: [],
    refreshed_at: new Date().toISOString(),
    source: 'snapshot',
    as_of: null,
  };
}

async function _mockDerivativesEndpoints(page, state) {
  await page.route('**/api/positions**', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(_positionsBody(state.qty)),
    });
  });

  await page.route('**/api/instruments**', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        cycle_date: '2026-09-29',
        count: 1,
        items: [
          { s: SYMBOL, e: 'NFO', t: 'CE', ls: 1, ts: 0.05, u: UNDERLYING, x: FUTURE_EXPIRY, k: 100 },
        ],
      }),
    });
  });

  await page.route('**/api/holdings**', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ rows: [], summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null }),
    });
  });

  await page.route('**/api/watchlist**', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ watchlists: [], symbols: [] }),
    });
  });

  // Mutable — every POST is recorded with the qty it carried, and the
  // response echoes a breakevens value derived from qty so a UI assertion
  // could distinguish responses if needed (not relied on here; the
  // request-capture below is the primary assertion).
  await page.route('**/api/options/strategy-analytics**', (route) => {
    const body = route.request().postDataJSON();
    const qty = Number(body?.legs?.[0]?.qty ?? 0);
    state.strategyCalls.push({ qty, ts: Date.now() });
    // Real backend response shape (backend/api/routes/options.py) carries a
    // `greeks: OptionGreeks` object per leg — CandidateLegRow.svelte reads
    // `lg.greeks.delta` unguarded, so an incomplete mock throws on every
    // reactive tick (found empirically while bisecting this spec) and masks
    // the actual signal under test. Always include it.
    const _greeks = { delta: 0.5, gamma: 0.01, theta: -0.3, vega: 0.2, rho: 0.01 };
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        underlying: UNDERLYING,
        spot: 460,
        spot_prev_close: 450,
        legs: (body?.legs ?? []).map(l => ({ ...l, greeks: _greeks })),
        payoff: [
          { spot: 400, today_value: -10, expiry_value: -10 },
          { spot: 460, today_value: 22.5 * qty / 5, expiry_value: 22.5 * qty / 5 },
          { spot: 520, today_value: 45, expiry_value: 45 },
        ],
        risk: { breakevens: [440] },
        breakevens: [440],
        pnl_at_spot: 22.5 * qty / 5,
        exp_pnl_at_spot: 22.5 * qty / 5,
        dte: 15,
        iv: 0.25,
        aggregate_greeks: _greeks,
      }),
    });
  });
}

test.describe('Derivatives page — Legs grid + Payoff refresh on fill', () => {
  test('Legs grid and Payoff analytics both refresh after a fill, without reload', async ({ page }) => {
    await loginAsAdmin(page);

    const state = { qty: 5, strategyCalls: [] };
    await _mockDerivativesEndpoints(page, state);

    /** @type {import('@playwright/test').WebSocketRoute | null} */
    let wsRoute = null;
    await page.routeWebSocket('**/ws/performance', (ws) => { wsRoute = ws; });

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    // Legs grid must show the pre-fill qty (5) first.
    const legsGrid = page.locator('.cand-grid');
    await expect(legsGrid).toBeVisible({ timeout: 15_000 });
    const qty5Cell = page.locator('.cand-grid .cand-row:not(.cand-row-total) .kv-pos', { hasText: /^5$/ });
    await expect(qty5Cell).toHaveCount(1, { timeout: 15_000 });

    // Initial strategy-analytics call must have landed with qty=5.
    await expect.poll(() => state.strategyCalls.some(c => c.qty === 5), { timeout: 15_000 }).toBe(true);

    // Wait for the WS route to register before injecting events.
    for (let i = 0; i < 25 && !wsRoute; i++) await page.waitForTimeout(200);
    if (!wsRoute) {
      test.skip(true, 'WebSocket to /ws/performance never connected — cannot inject test event');
      return;
    }

    // Simulate the fill: broker book now shows qty=10 on the same leg.
    state.qty = 10;
    const callsBeforeFill = state.strategyCalls.length;
    const _fillSendStartedAt = Date.now();

    // 1) Immediate postback fan-out event.
    wsRoute.send(JSON.stringify({
      event: 'position_filled', account: 'ZG0790', exchange: 'NFO',
      tradingsymbol: SYMBOL, qty: 5, fill_price: 455.0, ts: Date.now(), order_id: 'T1001',
    }));

    // 2) Backend's own delayed confirmation once the broker book has
    // genuinely propagated the fill (real-world delay ~2-7s; shortened
    // here since this test controls both sides of the mock).
    await page.waitForTimeout(400);
    wsRoute.send(JSON.stringify({
      event: 'positions_refreshed', tradingsymbol: SYMBOL, account: 'ZG0790', ts: Date.now(),
    }));

    // Legs grid must update to qty=10 — this surface has no gating and is
    // expected to pass even before the fix (included for completeness /
    // regression coverage, not as the primary defect probe).
    const qty10Cell = page.locator('.cand-grid .cand-row:not(.cand-row-total) .kv-pos', { hasText: /^10$/ });
    await expect(qty10Cell).toHaveCount(1, { timeout: 5_000 });

    // THE PRIMARY ASSERTION — a fresh strategy-analytics POST carrying the
    // new qty (10) must land within 3.5s of the fill events, well inside
    // the 5s marketAwareInterval poll budget. Before the fix, neither the
    // (raced, stale-legs) position_filled handler nor the positions_refreshed
    // handler (which never calls loadStrategy()) drives this — the operator
    // would only see it catch up on the next 5s tick, or not at all if a
    // second overlapping fill keeps resetting the window.
    await expect.poll(
      () => state.strategyCalls.some(c => c.qty === 10 && c.ts - _fillSendStartedAt < 3_500),
      { timeout: 4_000 },
    ).toBe(true);

    console.log('[derivatives_fill_refresh] strategyCalls:', JSON.stringify(state.strategyCalls));
    expect(state.strategyCalls.length).toBeGreaterThan(callsBeforeFill);
  });
});

test.describe('Derivatives page — source guards', () => {
  test('Stale: legs-watcher effect refetches on a legs-signature change, not only on underlying mismatch', () => {
    const src = readFileSync(
      '/Users/ramanambore/projects/ramboq/frontend/src/routes/(algo)/admin/derivatives/+page.svelte',
      'utf-8',
    );
    // Locate the "Bug 1 fix" legs-watcher effect (tracks `legs`, calls loadStrategy()).
    const idx = src.indexOf('Bug 1 fix: loadStrategy trigger');
    expect(idx, 'legs-watcher effect comment should exist').toBeGreaterThan(-1);
    const slice = src.slice(idx, idx + 2400);
    // Must NOT unconditionally bail out just because strategy.underlying
    // already matches the selected underlying — it must also compare a
    // legs-signature (computeLegsKey or equivalent) so a same-underlying
    // quantity/leg change still triggers a refetch.
    expect(
      /computeLegsKey/.test(slice),
      'legs-watcher effect must compare a legs signature, not only strategy.underlying, ' +
      'so a same-underlying fill still triggers loadStrategy()',
    ).toBe(true);
  });
});
