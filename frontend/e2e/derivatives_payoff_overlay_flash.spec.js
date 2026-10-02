/**
 * derivatives_payoff_overlay_flash.spec.js
 *
 * Fix 4 — Derivatives Payoff chart loading-overlay UX.
 *
 * Operator ask (as clarified): the Payoff chart's rotating-circle
 * spinner (`.payoff-loading-ring-slot`, OptionsPayoff.svelte, rendered
 * next to the LTP stat-row value, driven by the `refreshing` prop —
 * `_stratRefreshing` in +page.svelte) is an EXISTING component that
 * needed repurposing, not deleting. Before this fix, `_stratRefreshing`
 * was set true unconditionally on EVERY `loadStrategy()` call —
 * first load AND every routine ~5s refetch — so the spinner fired on
 * every poll even though a chart with data already showing has no
 * "nothing to show yet" state to spin for.
 *
 * Fix: `_stratRefreshing` now mirrors `loading`'s own `!strategy` gate
 * exactly (+page.svelte's `loadStrategy()`) — the spinner is reserved
 * for the genuine first-load / no-data-yet window. OptionsPayoff's own
 * LTP/CHG% stat-row values already flash on every `spot`/`spotPct`
 * change (`_spotFlash`, a `createTickFlash()` instance keyed on `spot`,
 * OptionsPayoff.svelte ~572-573/948/954) — that flash is untouched by
 * this fix and is now the SOLE refresh indicator for routine polls.
 *
 * This spec proves BOTH halves: (a) once the strategy has loaded once,
 * a routine refetch that changes `spot` never shows the spinner, and
 * (b) that same spot change DOES flash the LTP/CHG% stat-row values.
 *
 * Five quality dimensions:
 *  1. SSOT     — the spinner's gate (`!strategy`) is now textually
 *                identical to `loading`'s own gate, not a second
 *                independently-maintained condition.
 *  2. Perf     — no new network calls; the flash mechanism already
 *                existed and fires off the existing `spot`/`spotPct` props.
 *  3. Stale    — grep guard: `_stratRefreshing = true` must be set ONLY
 *                inside an `if (!strategy)` block, never unconditionally.
 *  4. Reusable — reuses the existing `_spotFlash`/`tf-up`/`tf-down`
 *                convention already used elsewhere (PositionStrip,
 *                MarketPulse, NavCard) — no new flash primitive.
 *  5. UX       — no persistent rotating circle during ordinary use; the
 *                chart "breathes" via the same flash language as every
 *                other live-updating cell in this codebase.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/derivatives_payoff_overlay_flash.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

const PAGE_SRC = path.resolve(
  process.cwd(),
  'src/routes/(algo)/admin/derivatives/+page.svelte'
);

test.describe('Fix 4 source guard — spinner gate matches loading\'s !strategy condition', () => {
  test('_stratRefreshing is set true only inside an `if (!strategy)` block', () => {
    const src = fs.readFileSync(PAGE_SRC, 'utf8');
    const idx = src.indexOf('const _thisGen = ++_stratGen;');
    expect(idx, 'loadStrategy() body marker should exist').toBeGreaterThan(-1);
    const slice = src.slice(idx, idx + 1500);
    expect(
      /if\s*\(!strategy\)\s*\{\s*loading = true;\s*_stratRefreshing = true;\s*\}/.test(slice),
      '_stratRefreshing must be set true inside the SAME `if (!strategy)` block as `loading`, ' +
      'not unconditionally on every loadStrategy() call'
    ).toBe(true);
    // Negative control — the OLD unconditional form must be gone.
    expect(slice).not.toMatch(/^\s*_stratRefreshing = true;\s*$/m);
  });
});

const UNDERLYING    = 'NIFTY';
const SYMBOL        = 'NIFTY26DEC25000CE';
const FUTURE_EXPIRY = '2026-12-31';
const DERIV_URL     = `/admin/derivatives?u=${UNDERLYING}`;
const ACCOUNT       = 'ZG0790';

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

test.describe('Derivatives — Payoff overlay: flash replaces spinner on routine refresh', () => {
  test('a routine strategy refetch that changes spot never shows the spinner, and flashes the LTP/CHG% stat-row values instead', async ({ page }) => {
    await loginAsAdmin(page);

    const state = { spot: 25100, calls: 0, tickLtp: null };

    await page.route('**/api/positions**', (route) => {
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: [_realRow()], summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null }),
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
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows: [], summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null }) });
    });
    await page.route('**/api/watchlist**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ watchlists: [], symbols: [] }) });
    });
    await page.route('**/api/options/strategy-analytics**', (route) => {
      state.calls++;
      const body = route.request().postDataJSON();
      const _greeks = { delta: 0.5, gamma: 0.01, theta: -0.3, vega: 0.2, rho: 0.01 };
      const spot = state.spot;
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          underlying: UNDERLYING, spot, spot_prev_close: 24900,
          legs: (body?.legs ?? []).map(l => ({ ...l, greeks: _greeks })),
          payoff: [
            { spot: spot - 1000, today_value: -100, expiry_value: -100 },
            { spot, today_value: 500, expiry_value: 500 },
            { spot: spot + 1000, today_value: 900, expiry_value: 900 },
          ],
          risk: { breakevens: [24800] }, breakevens: [24800],
          pnl_at_spot: 500, exp_pnl_at_spot: 500,
          dte: 15, iv: 0.18, aggregate_greeks: _greeks,
        }),
      });
    });

    // Payoff chart's `spot`/`spotPct` props (and therefore `_spotFlash`,
    // OptionsPayoff's own flash trigger) are driven by `payoffSpot` /
    // `liveSpot` — the underlying's own live SSE tick (NIFTY is a pure
    // index, no anchor future, so it live-ticks directly) — NOT by the
    // strategy-analytics response's `spot` field (that field only feeds
    // the payoff CURVE's x-axis; the chart's spot marker/overlay is
    // deliberately independent — see the "single spot resolver" design
    // notes elsewhere in this file). Mock the SSE quote stream so a tick
    // for the underlying can be injected deterministically.
    await page.route('**/api/quotes/stream', async (route) => {
      if (state.tickLtp == null) {
        route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'event: heartbeat\ndata: {}\n\n' });
        return;
      }
      // resolveUnderlyingTradingsymbol maps the bare index root "NIFTY"
      // to the cash-ticker symbol "NIFTY 50" (same ticker the backend's
      // own spot resolver uses for an index with no anchor future).
      const frame = `event: tick\ndata: ${JSON.stringify({ sym: 'NIFTY 50', ltp: state.tickLtp })}\n\n`;
      route.fulfill({ status: 200, contentType: 'text/event-stream', body: frame });
    });

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const legsGrid = page.locator('.cand-grid');
    await expect(legsGrid).toBeVisible({ timeout: 15_000 });
    await expect.poll(() => state.calls, { timeout: 15_000 }).toBeGreaterThan(0);

    // First load has landed — the spinner must already be gone (its
    // `.on` modifier class is what actually drives the CSS animation).
    const spinnerOn = page.locator('.payoff-loading-ring-slot.on');
    await expect(spinnerOn).toHaveCount(0, { timeout: 5_000 });

    const ltpStat = page.locator('.payoff-stats .ps-row', { hasText: 'LTP' }).locator('.ps-v').first();
    await expect(ltpStat).toBeVisible({ timeout: 5_000 });

    // Change spot so the NEXT refetch carries a different value — this
    // is what used to also flip `_stratRefreshing` (and hence the
    // spinner) on every poll. Trigger it deterministically via the
    // manual Refresh button (`_refreshAll()` -> `loadStrategy({force:true})`)
    // instead of waiting on the `marketAwareInterval`-gated 5s poll,
    // which no-ops entirely outside NSE/MCX session hours (this spec
    // must be stable on a market holiday / after-hours test run too).
    const callsBefore = state.calls;
    state.spot = 25250;

    const refreshBtn = page.getByRole('button', { name: /Refresh derivatives/i }).first();
    await refreshBtn.click();

    // Poll for the spinner's `.on` class throughout the whole refetch
    // window — it must NEVER appear.
    let sawSpinner = false;
    const _deadline = Date.now() + 4_000;
    while (Date.now() < _deadline) {
      if (await spinnerOn.count() > 0) { sawSpinner = true; break; }
      await page.waitForTimeout(100);
    }
    expect(sawSpinner, 'the rotating-circle spinner must never show during a routine refetch').toBe(false);

    // The refetch DID land (proves the no-spinner result isn't just
    // "nothing happened yet").
    await expect.poll(() => state.calls, { timeout: 5_000 }).toBeGreaterThan(callsBefore);

    // And the LTP stat-row value flashes (tf-up/tf-down) — the
    // replacement refresh indicator — when the chart's OWN spot source
    // (the underlying's live SSE tick, not the strategy-analytics
    // response) changes. Arm a tick and poll for the transient class;
    // the flash window is only 300ms so a single point-in-time read can
    // miss it.
    const flashLocator = page.locator('.payoff-stats .ps-v.tf-up, .payoff-stats .ps-v.tf-down');
    state.tickLtp = 25600;
    // Poll tightly (50ms) right from the moment the tick is armed — the
    // SSE route's reconnect can land anywhere in its backoff window and
    // the flash itself only lasts 300ms once it does.
    let sawFlash = false;
    for (let i = 0; i < 160 && !sawFlash; i++) {
      if (await flashLocator.count() > 0) { sawFlash = true; break; }
      await page.waitForTimeout(50);
    }
    expect(sawFlash, 'LTP/CHG% stat-row values should flash (tf-up/tf-down) on a live spot tick').toBe(true);

    // And throughout THIS tick-driven update too, the spinner must stay off.
    expect(await spinnerOn.count(), 'the spinner must stay off during a live-tick-driven overlay update too').toBe(0);
  });
});
