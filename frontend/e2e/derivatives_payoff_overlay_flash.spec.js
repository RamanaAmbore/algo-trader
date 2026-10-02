/**
 * derivatives_payoff_overlay_flash.spec.js
 *
 * Derivatives Payoff chart loading-overlay UX.
 *
 * History: the Payoff chart's rotating-circle spinner
 * (`.payoff-loading-ring-slot`/`.payoff-loading-ring`, OptionsPayoff.svelte,
 * rendered next to the LTP stat-row value, driven by the `refreshing` prop
 * — `_stratRefreshing` in +page.svelte) was first repurposed (narrowed to
 * fire only on genuine first-load, not on every routine ~5s refetch — see
 * git history for that intermediate revision). Operator then asked for the
 * spinner to be REMOVED ENTIRELY, not conditionally shown even on first
 * load. This fix:
 *   - Deletes the `.payoff-loading-ring-slot`/`.payoff-loading-ring`
 *     markup and CSS from OptionsPayoff.svelte, and the now-unused
 *     `refreshing` prop.
 *   - Deletes `_stratRefreshing` from +page.svelte (no other consumer —
 *     it only ever fed the now-removed `refreshing` prop) and the
 *     `refreshing={_stratRefreshing}` wiring at the OptionsPayoff callsite.
 *   - OptionsPayoff's own LTP/CHG% stat-row values still flash on every
 *     `spot`/`spotPct` change (`_spotFlash`, a `createTickFlash()`
 *     instance keyed on `spot`, OptionsPayoff.svelte ~572-573) — untouched
 *     by this fix, and the sole refresh indicator for routine polls now
 *     that the spinner is gone.
 *   - The genuine first-load / no-data-yet window still has its own text
 *     placeholder ("Resolving spot…", OptionsPayoff's
 *     `{:else if loading && (!payoff.length || spot == null)}` branch),
 *     unaffected by the spinner removal — no replacement indicator was
 *     added per the operator's explicit "completely removed" instruction.
 *
 * This spec proves, in two separate test.describe blocks: (a) the spinner
 * markup/CSS never renders at all, including during a genuine cold-start
 * first-load window held open deterministically, and (b) a routine
 * refetch that changes the underlying's live-tick spot still flashes the
 * LTP/CHG% stat-row values. (a) is the fix this spec guards; (b) is a
 * pre-existing, occasionally-flaky check (see its own describe block's
 * comment) kept separate so its flakiness never masks a regression in (a).
 *
 * Five quality dimensions:
 *  1. SSOT     — grep guard: no `_stratRefreshing`/`payoff-loading-ring`
 *                token remains anywhere in either source file.
 *  2. Perf     — no new network calls; the flash mechanism already
 *                existed and fires off the existing `spot`/`spotPct` props.
 *  3. Stale    — the removed prop/state/markup leave no dead references.
 *  4. Reusable — reuses the existing `_spotFlash`/`tf-up`/`tf-down`
 *                convention already used elsewhere (PositionStrip,
 *                MarketPulse, NavCard) — no new flash primitive.
 *  5. UX       — no rotating circle ever, in any state (cold-start or
 *                routine refresh); the chart "breathes" via the same
 *                flash language as every other live-updating cell.
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
const PAYOFF_SRC = path.resolve(process.cwd(), 'src/lib/OptionsPayoff.svelte');

test.describe('Source guard — spinner removed entirely, no dead references', () => {
  test('neither source file mentions the spinner markup/CSS/prop/state any more', () => {
    const pageSrc   = fs.readFileSync(PAGE_SRC, 'utf8');
    const payoffSrc = fs.readFileSync(PAYOFF_SRC, 'utf8');

    expect(pageSrc, '+page.svelte must no longer reference _stratRefreshing').not.toMatch(/_stratRefreshing/);
    expect(payoffSrc, 'OptionsPayoff.svelte must no longer reference the refreshing prop').not.toMatch(/\brefreshing\b/);
    for (const src of [pageSrc, payoffSrc]) {
      expect(src).not.toMatch(/payoff-loading-ring/);
    }
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

/** Wires the common positions/instruments/holdings/watchlist mocks shared
 *  by both tests below. Returns the mutable `state` object the caller
 *  uses to drive the strategy-analytics + SSE responses. */
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
    // Payoff chart's `spot`/`spotPct` props (and therefore `_spotFlash`,
    // OptionsPayoff's own flash trigger) are driven by `payoffSpot` /
    // `liveSpot` — the underlying's own live SSE tick (NIFTY is a pure
    // index, no anchor future, so it live-ticks directly) — NOT by the
    // strategy-analytics response's `spot` field (that field only feeds
    // the payoff CURVE's x-axis; the chart's spot marker/overlay is
    // deliberately independent — see the "single spot resolver" design
    // notes elsewhere in this file). Mock the SSE quote stream so a tick
    // for the underlying can be injected deterministically.
    page.route('**/api/quotes/stream', async (route) => {
      if (state.tickLtp == null) {
        route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'event: heartbeat\ndata: {}\n\n' });
        return;
      }
      // resolveUnderlyingTradingsymbol maps the bare index root "NIFTY"
      // to the cash-ticker symbol "NIFTY 50" (same ticker the backend's
      // own spot resolver uses for an index with no anchor future).
      const frame = `event: tick\ndata: ${JSON.stringify({ sym: 'NIFTY 50', ltp: state.tickLtp })}\n\n`;
      route.fulfill({ status: 200, contentType: 'text/event-stream', body: frame });
    }),
  ]);
}

function _strategyAnalyticsBody(body, spot) {
  const _greeks = { delta: 0.5, gamma: 0.01, theta: -0.3, vega: 0.2, rho: 0.01 };
  return JSON.stringify({
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
  });
}

test.describe('Derivatives — Payoff overlay: spinner never renders', () => {
  test('the spinner element never renders, including during a genuine cold-start first-load window held open on purpose', async ({ page }) => {
    await loginAsAdmin(page);

    const state = { spot: 25100, calls: 0, tickLtp: null };
    // Manually-released gate — the first strategy-analytics response is
    // held open until the test explicitly calls `release()`, so the
    // cold-start window (`loading` true, no payoff/spot yet — the exact
    // state that used to render the spinner before this fix) is
    // deterministically observable instead of racing a fixed timeout.
    let release;
    const held = new Promise((r) => { release = r; });

    await _armCommonRoutes(page, state);
    await page.route('**/api/options/strategy-analytics**', async (route) => {
      state.calls++;
      await held;
      const body = route.request().postDataJSON();
      route.fulfill({ status: 200, contentType: 'application/json', body: _strategyAnalyticsBody(body, state.spot) });
    });

    // The spinner element — markup, not just a modifier class — must
    // never exist in the DOM at all, from the very first paint onward.
    const spinnerEl = page.locator('.payoff-loading-ring-slot, .payoff-loading-ring');

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    // Gate on the request actually having landed (not a fixed sleep) —
    // the strategy-analytics request only fires after positions +
    // instruments resolve, so this proves we're genuinely inside the
    // cold-start window before asserting anything about it.
    await expect.poll(() => state.calls, { timeout: 15_000 }).toBeGreaterThan(0);

    // Still held open — this IS the cold-start / no-data-yet window
    // (the chart may show its own "Resolving spot…" text placeholder, or
    // — per the stale-while-revalidate design covered by
    // derivatives_payoff_stale_revalidate.spec.js — a client-side
    // intrinsic stub curve; either way, no replacement indicator was
    // added for the removed spinner, so neither is asserted here). The
    // spinner markup must not exist at all while the request is held.
    await expect(spinnerEl, 'the spinner element must never render during cold-start first-load').toHaveCount(0);

    release();

    const legsGrid = page.locator('.cand-grid');
    await expect(legsGrid).toBeVisible({ timeout: 15_000 });

    // First load has landed — the spinner element still must not exist.
    await expect(spinnerEl).toHaveCount(0, { timeout: 5_000 });

    const ltpStat = page.locator('.payoff-stats .ps-row', { hasText: 'LTP' }).locator('.ps-v').first();
    await expect(ltpStat).toBeVisible({ timeout: 5_000 });

    // Change spot so the NEXT refetch carries a different value. Trigger
    // it deterministically via the manual Refresh button (`_refreshAll()`
    // -> `loadStrategy({force:true})`) instead of waiting on the
    // `marketAwareInterval`-gated 5s poll, which no-ops entirely outside
    // NSE/MCX session hours (this spec must be stable on a market
    // holiday / after-hours test run too).
    const callsBefore = state.calls;
    state.spot = 25250;

    const refreshBtn = page.getByRole('button', { name: /Refresh derivatives/i }).first();
    await refreshBtn.click();

    // Poll for the spinner element throughout the whole refetch window —
    // it must NEVER appear.
    let sawSpinner = false;
    const _deadline = Date.now() + 4_000;
    while (Date.now() < _deadline) {
      if (await spinnerEl.count() > 0) { sawSpinner = true; break; }
      await page.waitForTimeout(100);
    }
    expect(sawSpinner, 'the rotating-circle spinner must never render during a routine refetch').toBe(false);

    // The refetch DID land (proves the no-spinner result isn't just
    // "nothing happened yet").
    await expect.poll(() => state.calls, { timeout: 5_000 }).toBeGreaterThan(callsBefore);
  });
});

test.describe('Derivatives — Payoff overlay: live-tick flash (pre-existing, unrelated to spinner removal)', () => {
  // NOTE: this check has been observed to fail intermittently in this
  // environment even against the unmodified pre-fix code (verified by
  // re-running this exact assertion against the stashed original source
  // during the spinner-removal fix) — most likely an SSE mock
  // reconnect/backoff timing issue in the local Playwright harness, not a
  // regression introduced by the spinner removal. Left unskipped and
  // reported honestly rather than papered over.
  test('the LTP/CHG% stat-row values flash (tf-up/tf-down) on a live spot tick', async ({ page }) => {
    await loginAsAdmin(page);

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

    const ltpStat = page.locator('.payoff-stats .ps-row', { hasText: 'LTP' }).locator('.ps-v').first();
    await expect(ltpStat).toBeVisible({ timeout: 5_000 });

    // The LTP stat-row value flashes (tf-up/tf-down) when the chart's OWN
    // spot source (the underlying's live SSE tick, not the
    // strategy-analytics response) changes. Arm a tick and poll for the
    // transient class; the flash window is only 300ms so a single
    // point-in-time read can miss it.
    const flashLocator = page.locator('.payoff-stats .ps-v.tf-up, .payoff-stats .ps-v.tf-down');
    state.tickLtp = 25600;
    let sawFlash = false;
    for (let i = 0; i < 160 && !sawFlash; i++) {
      if (await flashLocator.count() > 0) { sawFlash = true; break; }
      await page.waitForTimeout(50);
    }
    expect(sawFlash, 'LTP/CHG% stat-row values should flash (tf-up/tf-down) on a live spot tick').toBe(true);
  });
});
