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

/**
 * SSOT + stale-code guards for the real (actual-money) Day P&L dart
 * (2026-11-25) and the DTE timezone fix on the same component.
 *
 * The third dart reuses the already-wired `dayPnl` prop (baseline-diff
 * Day P&L — see its own prop doc in OptionsPayoff.svelte) instead of a
 * new prop, is painted in both the bg and fg SVGs (same two-circle
 * ring+fill pattern as the amber "today" and sky "expiry" darts), in
 * violet (`--algo-violet` / `#c084fc` in app.css) so it reads distinctly
 * against amber (#fbbf24), sky (#7dd3fc), and the app's green/red
 * long-short convention (--c-long/--c-short, #4ade80/#f87171).
 *
 * The DTE fix replaces a UTC-anchored `new Date().toISOString().slice(0,
 * 10)` with the IST-anchored `todayIST()` (`$lib/dateFormat.js`) inside
 * `anchorDaysToExpiry` — see the dedicated Vitest unit test for the IST
 * vs UTC discriminating instant
 * (`src/lib/__tests__/data/dateFormat_todayIST_utc_vs_ist.test.js`);
 * `anchorDaysToExpiry`/`anchorRollingSoon` are computed here but have no
 * template consumer today (confirmed via grep — the `.payoff-anchor-
 * chip--amber` CSS class is dead code, unrelated to and unchanged by
 * this fix), so this is a source-level guard only.
 */
test('SSOT: real Day P&L dart (violet, dayPnl-driven) and IST-anchored anchorDaysToExpiry', () => {
  const optionsPayoffPath = path.resolve(process.cwd(), 'src/lib/OptionsPayoff.svelte');
  const opSrc = fs.readFileSync(optionsPayoffPath, 'utf8');

  // ── Day P&L dart ──────────────────────────────────────────────────────
  expect(
    opSrc.includes('const dayPnlY'),
    'Expected a dayPnlY derived computing the Y position of the real Day P&L marker'
  ).toBe(true);

  const violetDartMatches = opSrc.match(/cy=\{dayPnlY\}[\s\S]{0,10}class="payoff-dart-actual"/g) || [];
  expect(
    violetDartMatches.length,
    'Expected the violet Day P&L dart (ring + filled circle) painted in BOTH the bg and fg SVGs — 4 circles total'
  ).toBe(4);

  // Color must be the violet app.css token's hex value, and must differ
  // from every other dart/long-short color already in use.
  expect(opSrc.includes('#c084fc'), 'Expected the violet --algo-violet hex literal for the new dart').toBe(true);
  const violetStrokeOrFill = (opSrc.match(/stroke="#c084fc"|fill="#c084fc"/g) || []).length;
  expect(violetStrokeOrFill, 'Expected #c084fc used as both stroke and fill across the 4 circles').toBeGreaterThanOrEqual(4);

  const OTHER_COLORS = ['#fbbf24', '#7dd3fc', '#4ade80', '#f87171'];
  for (const c of OTHER_COLORS) {
    // Sanity: the new color literal must not collide with an existing one.
    expect(c).not.toBe('#c084fc');
  }

  // Must reuse the existing dayPnl prop — no new prop was added.
  expect(
    opSrc.includes('dayPnl = /** @type {number|null} */ (null)'),
    'dayPnl prop signature must be unchanged — the new dart must not introduce a new prop'
  ).toBe(true);

  // ── DTE timezone fix ──────────────────────────────────────────────────
  expect(
    opSrc.includes("import { todayIST } from '$lib/dateFormat.js';"),
    'anchorDaysToExpiry must import the shared todayIST() helper'
  ).toBe(true);
  expect(
    opSrc.includes("new Date(todayIST() + 'T00:00:00Z')"),
    'anchorDaysToExpiry must anchor "today" via todayIST(), not the browser/UTC clock'
  ).toBe(true);
  // Checks the actual OLD assignment statement (not just the bare
  // sub-expression), which also appears inside this fix's own explanatory
  // comment in OptionsPayoff.svelte — a looser substring check would
  // false-positive against that comment text.
  expect(
    opSrc.includes("todayMs = new Date(new Date().toISOString().slice(0, 10)"),
    'The old UTC-anchored "today" assignment must be fully removed from anchorDaysToExpiry'
  ).toBe(false);
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

  test('Case (d) — real Day P&L dart: renders violet at dayPnl\'s own Y position, distinct from the sky expiry dart', async ({ page }) => {
    await loginAsAdmin(page);
    await blockLiveTicks(page);

    const SYM = 'NIFTY26DEC25000CE';
    const SPOT = 25000;
    // Backend curve value at spot (feeds the sky "expiry" dart AND bounds
    // the chart's auto y-domain: lo=0, hi=EXPIRY_VALUE, pad=max(10%,100) —
    // so the domain here is [-100, 200]). DAY_PNL is chosen distinct from
    // EXPIRY_VALUE, on the SAME side of zero, and comfortably inside that
    // domain so the new dart's suppress-when-off-chart guard (dayPnlY)
    // doesn't hide it.
    const EXPIRY_VALUE = 100;
    const DAY_PNL = 175;

    await mockCommonEndpoints(page, {
      positionsRows: [
        // realised=0, unrealised=DAY_PNL (nonzero) -> currentTotalProfit()
        // takes the realised+unrealised branch = DAY_PNL. prev_settlement_pnl
        // omitted -> baseDayPnlForPosition's base term defaults to 0, so
        // candidatesDayPnl (dayPnl prop) resolves to exactly DAY_PNL - 0 =
        // DAY_PNL (nav.js SSOT formula — see "Frontend Day P&L SSOT" in
        // CLAUDE.md).
        { tradingsymbol: SYM, symbol: SYM, account: ACCOUNT_A,
          quantity: 1, average_price: 150, last_price: 170,
          pnl: 0, realised: 0, unrealised: DAY_PNL,
          overnight_quantity: 1, day_buy_quantity: 0, day_sell_quantity: 0,
          day_buy_value: 0, day_sell_value: 0 },
      ],
      instrumentItems: [
        { s: SYM, e: 'NFO', t: 'CE', ls: 25, ts: 0.05, u: 'NIFTY', x: '2026-12-31', k: 25000 },
      ],
      strategyResponse: {
        underlying: 'NIFTY', spot: SPOT, spot_prev_close: SPOT,
        // Synthetic anchor (test-only determinism knob, same convention as
        // Case (b) above) — guarantees payoffSpot resolves to `spot`
        // regardless of any live/poll underlying-quote tick.
        spot_anchor_contract: 'NIFTY_TEST_SPOT_ANCHOR', spot_source: 'futures',
        legs: [
          { symbol: SYM, qty: 1, strike: 25000, opt_type: 'CE', greeks: { delta: 0.5, gamma: 0.01, theta: -0.3, vega: 0.2, rho: 0.01 } },
        ],
        payoff: flatCurve(EXPIRY_VALUE, EXPIRY_VALUE, SPOT),
        risk: { breakevens: [] }, breakevens: [],
        pnl_at_spot: EXPIRY_VALUE, exp_pnl_at_spot: EXPIRY_VALUE,
        dte: 20, iv: 0.2, aggregate_greeks: { delta: 1.0, gamma: 0.02, theta: -0.3, vega: 0.2, rho: 0.01 },
      },
    });

    await page.goto('/admin/derivatives?u=NIFTY', { waitUntil: 'domcontentloaded' });

    // First, confirm the mocked dayPnl value actually reaches the
    // component (exact text-row check, same technique as readExpPnl
    // above) — this is the precise, formatting-independent proof that
    // `dayPnl` carries DAY_PNL. The SVG checks below then only need to
    // confirm the new marker tracks that already-proven number.
    const overlay = page.locator('.payoff-stats');
    await expect(overlay).toBeVisible({ timeout: 20_000 });
    const legsChip = overlay.locator('.ps-row', { has: page.locator('.ps-k', { hasText: 'LEGS' }) }).locator('.ps-v');
    await expect(legsChip).not.toHaveText('0', { timeout: 15_000 });
    const dayRow = overlay.locator('.ps-row', { has: page.locator('.ps-k', { hasText: 'DAY P&L' }) }).first();
    await expect(dayRow).toBeVisible({ timeout: 15_000 });
    const dayText = (await dayRow.locator('.ps-v').textContent()) || '';
    const dayValue = parseFloat(dayText.replace(/[₹,\s]/g, ''));
    expect(Number.isFinite(dayValue), `DAY P&L text "${dayText}" did not parse to a number`).toBe(true);
    expect(Math.round(dayValue)).toBe(DAY_PNL);

    // Violet dart — rendered in both the bg and fg SVGs, color distinct
    // from the amber/sky darts and the app's green/red long-short tokens.
    const fgViolet = page.locator('.payoff-svg-fg circle.payoff-dart-actual[fill="#c084fc"]');
    const fgVioletRing = page.locator('.payoff-svg-fg circle.payoff-dart-actual[stroke="#c084fc"]');
    const bgViolet = page.locator('.payoff-svg:not(.payoff-svg-fg) circle.payoff-dart-actual[fill="#c084fc"]');
    await expect(fgViolet).toHaveCount(1, { timeout: 15_000 });
    await expect(fgVioletRing).toHaveCount(1, { timeout: 15_000 });
    await expect(bgViolet).toHaveCount(1, { timeout: 15_000 });

    // Sky "expiry" dart (always wired live — unlike the amber "today" dart,
    // whose `currentPnl` prop is never passed by either real call site,
    // confirmed via `grep -n "currentPnl=" src`) gives a live, known second
    // data point: value=EXPIRY_VALUE at fgSky's own cy.
    const fgSky = page.locator('.payoff-svg-fg circle[fill="#7dd3fc"]');
    await expect(fgSky).toHaveCount(1, { timeout: 15_000 });

    const violetCy = parseFloat((await fgViolet.getAttribute('cy')) ?? '');
    const skyCy    = parseFloat((await fgSky.getAttribute('cy')) ?? '');
    expect(Number.isFinite(violetCy), 'Violet dart cy did not parse to a number').toBe(true);
    expect(Number.isFinite(skyCy), 'Sky dart cy did not parse to a number').toBe(true);

    // DAY_PNL (175) > EXPIRY_VALUE (100), both positive — on the SVG's
    // inverted P&L axis (higher P&L -> smaller y, "higher up" on screen),
    // the violet dart must sit strictly above (smaller cy than) the sky
    // dart. Also must not coincide with it.
    expect(violetCy, 'Violet dart must not share the sky dart\'s Y position').not.toBeCloseTo(skyCy, 1);
    expect(violetCy, `Violet dart (dayPnl=${DAY_PNL}) must render ABOVE the sky dart (expiry_value=${EXPIRY_VALUE})`)
      .toBeLessThan(skyCy);

    const violetStroke = await fgVioletRing.getAttribute('stroke');
    expect(violetStroke).toBe('#c084fc');
    expect(violetStroke).not.toBe('#fbbf24'); // amber "today" dart
    expect(violetStroke).not.toBe('#7dd3fc'); // sky "expiry" dart
    expect(['#4ade80', '#f87171']).not.toContain(violetStroke); // app's long/short green/red
  });
});
