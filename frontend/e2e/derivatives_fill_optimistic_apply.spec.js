/**
 * derivatives_fill_optimistic_apply.spec.js
 *
 * Fix 1 — Derivatives doesn't optimistically apply `position_filled`.
 *
 * Before this fix, /admin/derivatives' `position_filled` WS handler only
 * used the event to toast-match an order id and then trigger a forced
 * refetch of `/api/positions` + `/api/options/strategy-analytics` — it
 * never wrote into the shared `provisionalPositions.svelte.js` store the
 * way `MarketPulse.svelte`'s own `applyFill` usage does
 * (MarketPulse.svelte ~1574-1600). So a brand-new leg (no existing real
 * position row for that symbol/account) was invisible on the Legs grid /
 * Payoff chart until the next `/api/positions?fresh=1` round-trip landed
 * — up to several seconds, and longer still for brokers whose postback
 * delivery is unreliable (Dhan/Groww, per CLAUDE.md).
 *
 * Fix: the handler now calls `applyFill(msg)` immediately (optimistic
 * apply), with a 60s safety-timeout fallback (`clearFill` + one more
 * forced refetch) if `positions_refreshed` never arrives. `candidatePositions`
 * already read `getProvisionalPositions()` and `buildCandidatePositions`
 * already rendered a `~` provisional chip for it — only the WRITE side
 * (calling `applyFill`) was missing.
 *
 * This spec proves the row appears BEFORE any REST response can have
 * supplied it: `/api/positions` is held on an empty book throughout the
 * whole test (including the `fresh=1` request fired right after the
 * fill), so the only thing that can explain the row appearing is the
 * optimistic provisional-apply path.
 *
 * A second test proves the de-dup guard added alongside Fix 1
 * (`_provisionalForCandidates` in +page.svelte, mirroring MarketPulse's
 * own `scopedPositions` dedup): when a REAL row for the same
 * (symbol, account) already exists, the provisional (~) row must NOT
 * also render as a second, double-counted line for that symbol.
 *
 * Five quality dimensions:
 *  1. SSOT     — the Legs grid's qty/badge agree with the WS payload,
 *                not a REST response that never carries the new leg.
 *  2. Perf     — the row is visible well under 1s of the WS event, not
 *                "eventually" on a 5s poll tick.
 *  3. Stale    — negative control: a `position_filled` for a symbol that
 *                ALREADY has a real row must not duplicate it.
 *  4. Reusable — reuses the exact mocking idiom from
 *                `derivatives_fill_refresh.spec.js` / `derivatives_fill_no_orderbook_mounted.spec.js`.
 *  5. UX       — operator sees the new leg the instant the fill lands,
 *                not after a manual refresh or a poll-cadence delay.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/derivatives_fill_optimistic_apply.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(60_000);

// NIFTY (not a synthetic "TESTFO" root) — deliberate: `?u=` seeds an
// underlying that has NO real position yet (the whole point of the
// brand-new-leg scenario), and the page's own auto-select $effect
// (+page.svelte ~1578-1584) resets `selectedUnderlying` back to
// `opts[0]` whenever the current pick isn't found in
// `underlyingOptionsForPicker` at all. A synthetic root with zero
// positions/holdings/watchlist membership is never in that list, so it
// gets silently reset before the fill event even arrives. NIFTY is
// POPULAR_UNDERLYINGS[0] — always present as a 'popular' tier option
// regardless of position state — so the pick survives.
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
    quantity: qty, opening_quantity: qty, overnight_quantity: qty,
    day_buy_quantity: 0, day_sell_quantity: 0, day_sell_value: 0,
    average_price: 450.5, last_price: 455.0, close_price: 450.0,
    pnl: 22.5, day_change_val: 5.0, unrealised_pnl: 22.5, realised_pnl: 0,
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
}

test.describe('Derivatives — optimistic apply for position_filled', () => {
  test('a brand-new leg appears immediately via the ~ provisional row, while /api/positions keeps serving an empty book throughout', async ({ page }) => {
    await loginAsAdmin(page);

    // The broker book NEVER includes this symbol for the whole test —
    // the only way the row can appear is the optimistic provisional path.
    const state = { rows: [] };
    await _mockCommonEndpoints(page, state);

    /** @type {import('@playwright/test').WebSocketRoute | null} */
    let wsRoute = null;
    await page.routeWebSocket('**/ws/performance', (ws) => { wsRoute = ws; });

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const legsGrid = page.locator('.cand-grid');
    await expect(legsGrid).toBeVisible({ timeout: 15_000 });

    // No candidate row for the symbol yet — confirms the starting state
    // really is empty before the fill event.
    await expect(page.locator('.cand-provisional-tag')).toHaveCount(0);

    for (let i = 0; i < 25 && !wsRoute; i++) await page.waitForTimeout(200);
    if (!wsRoute) {
      test.skip(true, 'WebSocket to /ws/performance never connected — cannot inject test event');
      return;
    }

    const _fillSentAt = Date.now();
    wsRoute.send(JSON.stringify({
      event: 'position_filled', account: ACCOUNT, exchange: 'NFO',
      tradingsymbol: SYMBOL, qty: 5, fill_price: 455.0, ts: Date.now(), order_id: 'T2001',
    }));

    // PRIMARY ASSERTION — the provisional (~) chip and the qty=5 cell
    // appear within ~1s, well before any 5s poll tick, and with
    // /api/positions still serving an empty book the whole time.
    await expect(page.locator('.cand-provisional-tag')).toHaveCount(1, { timeout: 2_000 });
    const elapsed = Date.now() - _fillSentAt;
    console.log(`[derivatives_fill_optimistic_apply] provisional row visible after ${elapsed}ms`);
    expect(elapsed).toBeLessThan(2_000);

    const qty5Cell = page.locator('.cand-grid .cand-row:not(.cand-row-total) .kv-pos', { hasText: /^5$/ });
    await expect(qty5Cell).toHaveCount(1, { timeout: 2_000 });
  });

  test('negative control: a fill on a symbol that already has a real row does not duplicate it', async ({ page }) => {
    await loginAsAdmin(page);

    // Real row already exists for the symbol (qty 5) — the dedup guard
    // (`_provisionalForCandidates`) must suppress the provisional entry.
    const state = { rows: [_realRow(5)] };
    await _mockCommonEndpoints(page, state);

    /** @type {import('@playwright/test').WebSocketRoute | null} */
    let wsRoute = null;
    await page.routeWebSocket('**/ws/performance', (ws) => { wsRoute = ws; });

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const legsGrid = page.locator('.cand-grid');
    await expect(legsGrid).toBeVisible({ timeout: 15_000 });
    const qty5Cell = page.locator('.cand-grid .cand-row:not(.cand-row-total) .kv-pos', { hasText: /^5$/ });
    await expect(qty5Cell).toHaveCount(1, { timeout: 15_000 });

    for (let i = 0; i < 25 && !wsRoute; i++) await page.waitForTimeout(200);
    if (!wsRoute) {
      test.skip(true, 'WebSocket to /ws/performance never connected — cannot inject test event');
      return;
    }

    wsRoute.send(JSON.stringify({
      event: 'position_filled', account: ACCOUNT, exchange: 'NFO',
      tradingsymbol: SYMBOL, qty: 5, fill_price: 455.0, ts: Date.now(), order_id: 'T2002',
    }));

    // Give the optimistic-apply + any re-render a moment, then assert no
    // second, provisional-tagged row ever showed up for this symbol —
    // exactly one row (the real one) at all times.
    await page.waitForTimeout(1_000);
    await expect(page.locator('.cand-provisional-tag')).toHaveCount(0);
    await expect(page.locator('.cand-grid .cand-row:not(.cand-row-total)')).toHaveCount(1);
  });
});
