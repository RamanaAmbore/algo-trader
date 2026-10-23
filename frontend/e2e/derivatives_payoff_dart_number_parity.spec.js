/**
 * derivatives_payoff_dart_number_parity.spec.js
 *
 * Regression for the C1 invariant ("chart value = marker position,
 * always" — CLAUDE.md "Payoff chart Exp P&L basis"): the Payoff chart's
 * expiry dart (drawn from the backend-computed curve,
 * `yOf(curveAtSpot.expiry_value)` in OptionsPayoff.svelte) and its own
 * printed Exp P&L number next to it used to be computed by two
 * genuinely different formulas.
 *
 * Root cause: `_chartExpPnlAtSpot` in +page.svelte independently summed
 * each enabled leg's Exp P&L via `_legExpPnlDisplay`/`expiryPnlWithRealised`
 * — a simple per-leg intrinsic-at-spot formula that cannot replicate three
 * things the BACKEND curve (`multileg_payoff_curve` /
 * `_leg_today_expiry_arrays`, backend/api/algo/derivatives.py) does:
 *
 *   (a) Multi-month MCX basket — the backend scales each leg's own spot
 *       by its `scale_ratio` before pricing; the frontend readout priced
 *       every leg at the SAME shared spot.
 *   (b) Calendar/diagonal spread — the backend Black-Scholes-prices a far
 *       leg at its OWN remaining time-to-expiry; the frontend readout
 *       used plain intrinsic value (T=0) for every leg.
 *   (c) A futures leg — the backend values it at the shared (scaled) grid
 *       spot; the frontend readout used `resolveExpiryAnchor()`, which
 *       prefers the leg's OWN polled LTP — a different, independently-
 *       ticking price.
 *
 * Fix (2026-10-23): `_chartExpPnlAtSpot` is deleted. The chart's Exp P&L
 * readout (OptionsPayoff.svelte) now ALWAYS reads `curveAtSpot.expiry_value`
 * — the exact value interpolated from the SAME backend `payoff` curve +
 * `expiryPnlOffset` the dart's own y-position already uses. There is no
 * second computation left to drift.
 *
 * Test strategy: each case fully mocks `/api/options/strategy-analytics`
 * to return a FLAT expiry curve (identical value at every grid point) —
 * removing any dependency on exactly which spot tick lands — set to a
 * value that deliberately differs from what the OLD local per-leg sum
 * would have computed from the same mocked position rows. Pre-fix, the
 * displayed Exp P&L equals the local sum (confirmed by temporarily
 * reverting the fix locally — see PLAN.md cluster 3C). Post-fix it must
 * equal the mocked backend curve value.
 *
 * `spot_anchor_contract` is set to a symbol no SSE tick ever arrives for
 * (ticks are blocked entirely) so `payoffSpot` deterministically falls to
 * `strategy.spot` (payoffSpot's Tier-2 backend-resolved fallback) on every
 * poll, regardless of exchange — a test-only determinism knob, not a
 * claim that NSE options really have a futures anchor.
 *
 * Five quality dimensions:
 *  1. SSOT   — asserts the printed number equals the SAME backend curve
 *              value driving the dart, not a reimplementation of either
 *              formula.
 *  2. Perf   — no extra polling; reuses the page's normal 5s refresh.
 *  3. Stale  — grep guard confirms `_chartExpPnlAtSpot`/`legsExpPnlAtSpot`
 *              are fully gone from both +page.svelte and OptionsPayoff.svelte.
 *  4. Reuse  — mocks the same REST endpoints (positions/instruments/
 *              holdings/watchlist/strategy-analytics) every other
 *              derivatives spec already uses (see derivatives_leg_coherence
 *              .spec.js's `_mockCommonEndpoints` for the proven pattern).
 *  5. UX     — Exp P&L row stays right-aligned / pos-neg colored
 *              regardless of which source feeds it.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/derivatives_payoff_dart_number_parity.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

test.setTimeout(60_000);

// ── Stale-code / SSOT grep guards (static source scan, no browser) ────────

test('SSOT: _chartExpPnlAtSpot and legsExpPnlAtSpot are fully removed, not just unused', () => {
  const pagePath = path.resolve(process.cwd(), 'src/routes/(algo)/admin/derivatives/+page.svelte');
  const optionsPayoffPath = path.resolve(process.cwd(), 'src/lib/OptionsPayoff.svelte');
  const pageSrc = fs.readFileSync(pagePath, 'utf8');
  const opSrc = fs.readFileSync(optionsPayoffPath, 'utf8');

  expect(
    pageSrc.includes('const _chartExpPnlAtSpot'),
    '_chartExpPnlAtSpot derived must be deleted, not left dead'
  ).toBe(false);
  expect(
    pageSrc.includes('legsExpPnlAtSpot={'),
    '+page.svelte must no longer pass a legsExpPnlAtSpot prop to OptionsPayoff'
  ).toBe(false);
  expect(
    opSrc.includes('legsExpPnlAtSpot'),
    'OptionsPayoff.svelte must no longer accept a legsExpPnlAtSpot prop/override'
  ).toBe(false);

  // The printed Exp P&L number must read curveAtSpot.expiry_value directly
  // — the exact expression the dart's own y-position uses.
  const expPnlReadoutMatch = opSrc.match(/Exp P&amp;L[\s\S]{0,400}fmtMoney\(([^)]*)\)/);
  expect(expPnlReadoutMatch, 'Could not locate the Exp P&L readout\'s fmtMoney(...) call').not.toBeNull();
  expect(
    expPnlReadoutMatch[1].trim(),
    'Exp P&L readout must render fmtMoney(curveAtSpot.expiry_value) — the same value the dart is drawn at'
  ).toBe('curveAtSpot.expiry_value');

  const dartLines = opSrc.match(/cy=\{yOf\(curveAtSpot\.expiry_value\)\}/g) || [];
  expect(dartLines.length, 'Expected the expiry dart to still be drawn from curveAtSpot.expiry_value')
    .toBeGreaterThanOrEqual(1);
});

// ── Live-UI divergence cases ───────────────────────────────────────────────

const ACCOUNT_A = 'ZG0790';
const ACCOUNT_B = 'ZG0791';

function positionsBody(rows) {
  return { rows, summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null };
}

/** @param {{symbol:string,account:string,qty:number,avgCost:number,ltp:number}} opts */
function optionRow({ symbol, account, qty, avgCost, ltp }) {
  return {
    tradingsymbol: symbol, symbol, account,
    quantity: qty, average_price: avgCost, last_price: ltp,
    pnl: 0, realised: 0, unrealised: 0,
    overnight_quantity: qty, day_buy_quantity: 0, day_sell_quantity: 0,
    day_buy_value: 0, day_sell_value: 0,
  };
}

/** @param {{symbol:string,account:string,qty:number,avgCost:number,ltp:number}} opts */
function futRow({ symbol, account, qty, avgCost, ltp }) {
  return optionRow({ symbol, account, qty, avgCost, ltp });
}

async function blockLiveTicks(page) {
  await page.route('**/api/quotes/stream', (route) => route.abort());
  await page.routeWebSocket('**/ws/performance', (ws) => ws.close());
  await page.routeWebSocket('**/ws/algo', (ws) => ws.close());
}

/**
 * @param {import('@playwright/test').Page} page
 * @param {{
 *   positionsRows: any[],
 *   instrumentItems: any[],
 *   strategyResponse: any,
 * }} opts
 */
async function mockCommonEndpoints(page, { positionsRows, instrumentItems, strategyResponse }) {
  await page.route('**/api/positions**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(positionsBody(positionsRows)) });
  });

  await page.route('**/api/instruments**', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ cycle_date: '2026-10-23', count: instrumentItems.length, items: instrumentItems }),
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
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(strategyResponse) });
  });
}

/** Reads the Payoff overlay's Exp P&L row and returns the parsed numeric
 *  value (strips ₹, commas, whitespace; keeps the leading sign). */
async function readExpPnl(page) {
  const overlay = page.locator('.payoff-stats');
  await expect(overlay).toBeVisible({ timeout: 20_000 });
  const expRow = overlay.locator('.ps-row', { has: page.locator('.ps-k', { hasText: 'Exp P&L' }) }).first();
  await expect(expRow).toBeVisible({ timeout: 15_000 });
  const valEl = expRow.locator('.ps-v');
  // Give the strategy-analytics round trip + reactive chain a moment —
  // same cadence derivatives_leg_coherence.spec.js budgets. A bare
  // non-empty check isn't enough: the client-side stub curve renders a
  // transient "₹0.00" before the mocked strategy-analytics response
  // lands, so wait for the LEGS chip to confirm the real strategy (not
  // the stub) is active before reading the value.
  const legsChip = page.locator('.payoff-stats').locator('.ps-row', { has: page.locator('.ps-k', { hasText: 'LEGS' }) }).locator('.ps-v');
  await expect(legsChip).not.toHaveText('0', { timeout: 15_000 });
  const text = (await valEl.textContent()) || '';
  const cleaned = text.replace(/[₹,\s]/g, '');
  const n = parseFloat(cleaned);
  return { text, value: n };
}

/** Flat payoff curve — identical expiry_value (and today_value) at every
 *  grid point, so the test's assertion is immune to exactly which spot
 *  tick `payoffSpot` has landed on. */
function flatCurve(expiryValue, todayValue, spotCenter) {
  const pts = [spotCenter * 0.8, spotCenter, spotCenter * 1.2];
  return pts.map((spot) => ({ spot, today_value: todayValue, expiry_value: expiryValue }));
}

test.describe('Payoff chart dart-vs-number parity (C1 invariant)', () => {

  test('Case (a) — multi-month MCX basket: printed Exp P&L matches the backend curve, not a scale_ratio-blind local sum', async ({ page }) => {
    await loginAsAdmin(page);
    await blockLiveTicks(page);

    const NEAR = 'CRUDEOIL26NOV5500CE';
    const FAR  = 'CRUDEOIL26DEC5500CE';
    const SPOT = 5500;

    // Local per-leg intrinsic sum the OLD code computed (for documentation —
    // not asserted directly, since this spec targets dev/live code only):
    //   near: (max(0,5500-5500) - 50) * 1  = -50
    //   far:  (max(0,5500-5500) - 80) * 1  = -80
    //   local total = -130
    // Backend (mocked) truth — reflects the far leg's own scale_ratio-
    // adjusted contract price:
    const BACKEND_EXP_PNL = 145;
    const LOCAL_SUM_WOULD_BE = -130;

    await mockCommonEndpoints(page, {
      positionsRows: [
        optionRow({ symbol: NEAR, account: ACCOUNT_A, qty: 1, avgCost: 50, ltp: 40 }),
        optionRow({ symbol: FAR,  account: ACCOUNT_A, qty: 1, avgCost: 80, ltp: 70 }),
      ],
      instrumentItems: [
        { s: NEAR, e: 'MCX', t: 'CE', ls: 1, ts: 0.10, u: 'CRUDEOIL', x: '2026-11-18', k: 5500 },
        { s: FAR,  e: 'MCX', t: 'CE', ls: 1, ts: 0.10, u: 'CRUDEOIL', x: '2026-12-18', k: 5500 },
      ],
      strategyResponse: {
        underlying: 'CRUDEOIL', spot: SPOT, spot_prev_close: SPOT,
        spot_anchor_contract: 'CRUDEOIL26NOVFUT', spot_source: 'futures',
        legs: [
          { symbol: NEAR, qty: 1, strike: 5500, opt_type: 'CE', greeks: { delta: 0.5, gamma: 0.01, theta: -0.3, vega: 0.2, rho: 0.01 } },
          { symbol: FAR,  qty: 1, strike: 5500, opt_type: 'CE', greeks: { delta: 0.5, gamma: 0.01, theta: -0.3, vega: 0.2, rho: 0.01 } },
        ],
        payoff: flatCurve(BACKEND_EXP_PNL, BACKEND_EXP_PNL, SPOT),
        risk: { breakevens: [SPOT] }, breakevens: [SPOT],
        pnl_at_spot: BACKEND_EXP_PNL, exp_pnl_at_spot: BACKEND_EXP_PNL,
        dte: 26, iv: 0.3, aggregate_greeks: { delta: 1.0, gamma: 0.02, theta: -0.6, vega: 0.4, rho: 0.02 },
      },
    });

    await page.goto('/admin/derivatives?u=CRUDEOIL', { waitUntil: 'domcontentloaded' });

    const { text, value } = await readExpPnl(page);
    expect(Number.isFinite(value), `Exp P&L text "${text}" did not parse to a number`).toBe(true);
    expect(
      Math.round(value),
      `Exp P&L showed ${text} — expected the backend curve value (${BACKEND_EXP_PNL}), ` +
      `not the scale_ratio-blind local sum (${LOCAL_SUM_WOULD_BE}) the pre-fix code computed`
    ).toBe(BACKEND_EXP_PNL);
  });

  test('Case (b) — calendar/diagonal spread: printed Exp P&L matches the backend curve, not an intrinsic-only local sum', async ({ page }) => {
    await loginAsAdmin(page);
    await blockLiveTicks(page);

    const NEAR = 'NIFTY26OCT25000CE'; // sold, near expiry
    const FAR  = 'NIFTY26NOV25000CE'; // bought, far expiry, same strike
    const SPOT = 25000;

    // Local intrinsic-only sum the OLD code computed (documentation only):
    //   near (short): (max(0,25000-25000) - 120) * -1 =  120
    //   far  (long):  (max(0,25000-25000) - 250) *  1 = -250
    //   local total = -130
    // Backend (mocked) truth — the far leg still carries real time value
    // at the near leg's expiry (Black-Scholes at remaining T), not zero:
    const BACKEND_EXP_PNL = 300;
    const LOCAL_SUM_WOULD_BE = -130;

    await mockCommonEndpoints(page, {
      positionsRows: [
        optionRow({ symbol: NEAR, account: ACCOUNT_A, qty: -1, avgCost: 120, ltp: 5 }),
        optionRow({ symbol: FAR,  account: ACCOUNT_B, qty: 1,  avgCost: 250, ltp: 230 }),
      ],
      instrumentItems: [
        { s: NEAR, e: 'NFO', t: 'CE', ls: 25, ts: 0.05, u: 'NIFTY', x: '2026-10-29', k: 25000 },
        { s: FAR,  e: 'NFO', t: 'CE', ls: 25, ts: 0.05, u: 'NIFTY', x: '2026-11-26', k: 25000 },
      ],
      strategyResponse: {
        underlying: 'NIFTY', spot: SPOT, spot_prev_close: SPOT,
        // Synthetic anchor (test-only determinism knob — see file header):
        // guarantees payoffSpot resolves to `spot` above regardless of any
        // live/poll underlying-quote tick, with no real-world claim that
        // NIFTY options price off a futures anchor.
        spot_anchor_contract: 'NIFTY_TEST_SPOT_ANCHOR', spot_source: 'futures',
        multi_expiry: true,
        legs: [
          { symbol: NEAR, qty: -1, strike: 25000, opt_type: 'CE', greeks: { delta: -0.5, gamma: 0.01, theta: 0.3, vega: -0.2, rho: -0.01 } },
          { symbol: FAR,  qty: 1,  strike: 25000, opt_type: 'CE', greeks: { delta: 0.5,  gamma: 0.01, theta: -0.3, vega: 0.2, rho: 0.01 } },
        ],
        payoff: flatCurve(BACKEND_EXP_PNL, BACKEND_EXP_PNL, SPOT),
        risk: { breakevens: [SPOT] }, breakevens: [SPOT],
        pnl_at_spot: BACKEND_EXP_PNL, exp_pnl_at_spot: BACKEND_EXP_PNL,
        dte: 6, iv: 0.15, aggregate_greeks: { delta: 0.0, gamma: 0.0, theta: 0.0, vega: 0.0, rho: 0.0 },
      },
    });

    await page.goto('/admin/derivatives?u=NIFTY', { waitUntil: 'domcontentloaded' });

    const { text, value } = await readExpPnl(page);
    expect(Number.isFinite(value), `Exp P&L text "${text}" did not parse to a number`).toBe(true);
    expect(
      Math.round(value),
      `Exp P&L showed ${text} — expected the backend curve value (${BACKEND_EXP_PNL}), ` +
      `not the intrinsic-only local sum (${LOCAL_SUM_WOULD_BE}) the pre-fix code computed`
    ).toBe(BACKEND_EXP_PNL);
  });

  test('Case (c) — futures leg: printed Exp P&L matches the backend (payoffSpot-scaled) curve, not the leg\'s own polled LTP', async ({ page }) => {
    await loginAsAdmin(page);
    await blockLiveTicks(page);

    const FUT_SYM = 'NATURALGAS26NOVFUT';
    const PAYOFF_SPOT = 400;   // backend-resolved anchor spot (tick-driven in reality)
    const OWN_POLLED_LTP = 300; // this leg's own last traded price from the positions poll
    const AVG_COST = 250;

    // Local formula (resolveExpiryAnchor) picks the FUTURE'S OWN polled
    // LTP over the shared payoffSpot — documentation only:
    //   local = (300 - 250) * 1 = 50
    // Backend (mocked) truth — values the leg at the shared (scaled)
    // grid spot the dart is drawn from:
    //   backend = (400 - 250) * 1 = 150
    const BACKEND_EXP_PNL = PAYOFF_SPOT - AVG_COST; // 150
    const LOCAL_SUM_WOULD_BE = OWN_POLLED_LTP - AVG_COST; // 50

    await mockCommonEndpoints(page, {
      positionsRows: [
        futRow({ symbol: FUT_SYM, account: ACCOUNT_A, qty: 1, avgCost: AVG_COST, ltp: OWN_POLLED_LTP }),
      ],
      instrumentItems: [
        { s: FUT_SYM, e: 'MCX', t: 'FUT', ls: 1, ts: 0.10, u: 'NATURALGAS', x: '2026-11-25' },
      ],
      strategyResponse: {
        underlying: 'NATURALGAS', spot: PAYOFF_SPOT, spot_prev_close: PAYOFF_SPOT,
        spot_anchor_contract: FUT_SYM, spot_source: 'futures',
        legs: [
          { symbol: FUT_SYM, qty: 1, kind: 'fut' },
        ],
        payoff: flatCurve(BACKEND_EXP_PNL, BACKEND_EXP_PNL, PAYOFF_SPOT),
        risk: { breakevens: [] }, breakevens: [],
        pnl_at_spot: BACKEND_EXP_PNL, exp_pnl_at_spot: BACKEND_EXP_PNL,
        dte: 33, iv: null, aggregate_greeks: { delta: 1.0, gamma: 0, theta: 0, vega: 0, rho: 0 },
      },
    });

    await page.goto('/admin/derivatives?u=NATURALGAS', { waitUntil: 'domcontentloaded' });

    const { text, value } = await readExpPnl(page);
    expect(Number.isFinite(value), `Exp P&L text "${text}" did not parse to a number`).toBe(true);
    expect(
      Math.round(value),
      `Exp P&L showed ${text} — expected the backend curve value (${BACKEND_EXP_PNL}), ` +
      `not the futures-leg's-own-LTP local value (${LOCAL_SUM_WOULD_BE}) the pre-fix code computed`
    ).toBe(BACKEND_EXP_PNL);
  });
});
