/**
 * options_payoff_chg_flash_direction.spec.js
 *
 * OptionsPayoff.svelte's CHG% stat-row chip (`.payoff-stats .ps-row` with
 * "CHG%") used to colour its transient tick-flash from the SIGN of
 * `spotPct` (today's aggregate % move vs. yesterday's close) instead of the
 * actual direction of the most recent tick. A down-tick occurring on an
 * up day (spot still above prevClose, but lower than the previous tick)
 * incorrectly flashed GREEN (`ltp-tc-flash-up*`) instead of red — and vice
 * versa on a down day.
 *
 * Fix (2026-10-19): `_chgDir`, a new `$derived` in OptionsPayoff.svelte,
 * reads `_spotFlash.classOf('chg')` — the SAME tick-to-tick diff the
 * `_spotFlash.update('chg', spotPct)` effect already uses to decide
 * whether to flash at all — and maps it back to a plain 'up'/'down' for
 * `_tcFlashClass()`. The chip's flash direction now always matches the
 * direction that actually triggered it, independent of whether the day's
 * overall move is positive or negative.
 *
 * Five quality dimensions:
 *  1. SSOT     — flash direction is read from the same `_spotFlash`
 *                tracker that gates whether a flash fires at all; no
 *                second, independent sign computation.
 *  2. Perf     — no new network calls; reuses the existing SSE tick path.
 *  3. Stale    — n/a (no removed/dead state left behind by this fix).
 *  4. Reusable — same `createTickFlash()` / `_tcFlashClass()` convention
 *                already used by the SPOT row directly above this chip.
 *  5. UX       — the chip's flash colour always agrees with the direction
 *                of the tick that caused it, never with the day's
 *                aggregate sign — matching every other correct flash site
 *                in the app (Pulse LTP, Legs LTP, etc).
 *
 * Run:
 *   cd frontend && npx playwright test e2e/options_payoff_chg_flash_direction.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

const PAYOFF_SRC = path.resolve(process.cwd(), 'src/lib/OptionsPayoff.svelte');

test.describe('Source guard — CHG% chip flash direction comes from the real tick, not spotPct sign', () => {
  test('OptionsPayoff.svelte no longer colours the CHG% flash off spotPct\'s own sign', () => {
    const src = fs.readFileSync(PAYOFF_SRC, 'utf8');
    // The old buggy expression — must be gone.
    expect(src, 'must not derive CHG% flash direction from spotPct\'s sign').not.toMatch(
      /_tcFlashClass\(spotPct\s*>=\s*0\s*\?\s*'up'\s*:\s*'down'/
    );
    // The new real-direction derived must exist and feed the chip.
    expect(src).toMatch(/_chgDir\s*=\s*\$derived\.by/);
    expect(src).toMatch(/_tcFlashClass\(_chgDir,/);
  });
});

const UNDERLYING    = 'NIFTY';
const SYMBOL        = 'NIFTY26DEC25000CE';
const FUTURE_EXPIRY = '2026-12-31';
const DERIV_URL     = `/admin/derivatives?u=${UNDERLYING}`;
const ACCOUNT       = 'ZG0790';
const PREV_CLOSE    = 24900;

function _realRow() {
  return {
    tradingsymbol: SYMBOL, symbol: SYMBOL, exchange: 'NFO',
    quantity: 50, opening_quantity: 50, overnight_quantity: 50,
    day_buy_quantity: 0, day_sell_quantity: 0, day_sell_value: 0,
    average_price: 200.0, last_price: 210.0, close_price: 195.0,
    pnl: 500.0, day_change_val: 50.0, unrealised_pnl: 500.0, realised_pnl: 0,
    account: ACCOUNT, source: 'live',
  };
}

function _armCommonRoutes(page, state) {
  return Promise.all([
    page.route('**/api/positions**', (route) => {
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: [_realRow()], summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null }),
      });
    }),
    page.route('**/api/instruments**', (route) => {
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          cycle_date: '2026-09-29', count: 1,
          items: [{ s: SYMBOL, e: 'NFO', t: 'CE', ls: 50, ts: 0.05, u: UNDERLYING, x: FUTURE_EXPIRY, k: 25000 }],
        }),
      });
    }),
    page.route('**/api/holdings**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows: [], summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null }) });
    }),
    page.route('**/api/watchlist**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ watchlists: [], symbols: [] }) });
    }),
    // Same SSE-mock idiom as derivatives_payoff_overlay_flash.spec.js —
    // the chart's `spot`/`spotPct` props are driven by the underlying's
    // own live tick, not by the strategy-analytics response.
    page.route('**/api/quotes/stream', async (route) => {
      if (state.tickLtp == null) {
        route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'event: heartbeat\ndata: {}\n\n' });
        return;
      }
      const frame = `event: tick\ndata: ${JSON.stringify({ sym: 'NIFTY 50', ltp: state.tickLtp })}\n\n`;
      route.fulfill({ status: 200, contentType: 'text/event-stream', body: frame });
    }),
  ]);
}

function _strategyAnalyticsBody(body, spot) {
  const _greeks = { delta: 0.5, gamma: 0.01, theta: -0.3, vega: 0.2, rho: 0.01 };
  return JSON.stringify({
    underlying: UNDERLYING, spot, spot_prev_close: PREV_CLOSE,
    legs: (body?.legs ?? []).map(l => ({ ...l, greeks: _greeks })),
    payoff: [
      { spot: spot - 1000, today_value: -100, expiry_value: -100 },
      { spot, today_value: 500, expiry_value: 500 },
      { spot: spot + 1000, today_value: 900, expiry_value: 900 },
    ],
    risk: { breakevens: [24800] }, breakevens: [24800],
    pnl_at_spot: 500, exp_pnl_at_spot: 500,
    dte: 15, iv: 0.18, aggregate_greeks: _greeks,
  });
}

test.describe('Derivatives — Payoff CHG% chip flashes the real tick direction, not the day-sign', () => {
  test('a down-tick on an up day flashes red (down), never green (up)', async ({ page }) => {
    test.setTimeout(60_000);
    await loginAsAdmin(page);

    // Spot starts well above prevClose (24900) — an "up day" throughout
    // this test, so the OLD buggy sign-of-spotPct logic would always
    // paint 'up' regardless of tick direction.
    const state = { spot: 25100, calls: 0, tickLtp: null };
    await _armCommonRoutes(page, state);
    await page.route('**/api/options/strategy-analytics**', async (route) => {
      state.calls++;
      const body = route.request().postDataJSON();
      route.fulfill({ status: 200, contentType: 'application/json', body: _strategyAnalyticsBody(body, state.spot) });
    });

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });
    const legsGrid = page.locator('.cand-grid');
    await expect(legsGrid).toBeVisible({ timeout: 15_000 });
    await expect.poll(() => state.calls, { timeout: 15_000 }).toBeGreaterThan(0);

    const chgRow  = page.locator('.payoff-stats .ps-row', { hasText: 'CHG%' });
    const chgChip = chgRow.locator('.ps-v').first();
    await expect(chgChip).toBeVisible({ timeout: 5_000 });
    // Mount's initial SPOT can come from a persisted/real quote cache
    // (symbolStore/persistentCache), not from this test's own SSE mock —
    // so the baseline _spotFlash eventually tracks must be pinned
    // deterministically via a KNOWN, confirmed tick rather than assumed.
    const spotChip = page.locator('.payoff-stats .ps-row', { hasText: 'LTP' }).locator('.ps-v').first();

    // First tick — pins a KNOWN baseline (25,300) for _spotFlash's 'chg'
    // key. Confirmed via the SPOT readout itself actually reflecting this
    // tick (not a fixed timeout) before proceeding.
    //
    // NOTE on timeout generosity: the mocked `/api/quotes/stream` route
    // completes each fulfilled response immediately, so the browser's
    // EventSource only picks up a changed `state.tickLtp` on its next
    // natural reconnect — timing that is NOT fully deterministic in this
    // harness (same documented class of flakiness as
    // derivatives_payoff_overlay_flash.spec.js's own live-tick-flash
    // test: "observed to fail intermittently... most likely an SSE mock
    // reconnect/backoff timing issue, not a regression"). A generous
    // timeout reduces (does not eliminate) that flakiness.
    state.tickLtp = 25300;
    await expect.poll(async () => await spotChip.textContent(), { timeout: 20_000 }).toContain('25,300');
    // Let the flash from this first (possibly direction-changing) tick
    // fully settle before measuring the one that matters.
    await page.waitForTimeout(500);

    // Second tick — spot DROPS a bit from the pinned 25,300 baseline but
    // stays well above prevClose (24900), so spotPct stays positive
    // (still an "up day" by the old buggy sign check) while the real
    // tick direction is DOWN.
    state.tickLtp = 24950;

    let sawClass = null;
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline && !sawClass) {
      const cls = await chgChip.getAttribute('class');
      if (cls && /ltp-tc-flash-(up|down)/.test(cls)) {
        sawClass = /ltp-tc-flash-up/.test(cls) ? 'up' : 'down';
        break;
      }
      await page.waitForTimeout(40);
    }

    expect(sawClass, 'CHG% chip should have flashed on the down-tick').toBe('down');
  });
});
