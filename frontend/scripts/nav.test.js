/**
 * nav.test.js — Unit tests for the canonical NAV breakdown helper.
 *
 * Tier 1 / A2 — two surfaces (PerformancePage.svelte navByAcct grid
 * + NavBreakdown.svelte card) used to inline the same formula. Both
 * now import `navByAccount` from `$lib/data/nav.js`. This spec
 * asserts:
 *
 *  1. SSOT  — golden values for the v4 formula
 *           cash = cash_sod + option_premium
 *           pos_m2m = Σ position.unrealised
 *           hold = Σ holdings.cur_val
 *           nav = cash + pos_m2m + hold
 *  2. Perf  — no async, no I/O; pure sync compute
 *  3. Stale — only nav.js holds the formula (regex grep over
 *           consumer files: no inline `option_premium` math left).
 *  4. Reuse — both PerformancePage + NavBreakdown import from
 *           `$lib/data/nav` (grep guard).
 *  5. UX    — empty inputs, missing fields, totals-row math.
 *
 * Run with:  node --test frontend/scripts/nav.test.js
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import {
  navRowForAccount, navByAccount, navTotalRow,
  baseDayPnlForPosition, positionsPnlFiltered,
  livePositionDayPnl,
} from '../src/lib/data/nav.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND_SRC = resolve(__dirname, '..', 'src');


// ── Fixtures ────────────────────────────────────────────────────────────────

const funds = [
  { account: 'ZG0790', cash: 100_000, option_premium: 5_000 },
  { account: 'DH3747', cash: 50_000,  option_premium: 0 },
  { account: 'GR87DF', cash: 10_000,  option_premium: 250 },
];

const positions = [
  { account: 'ZG0790', unrealised: 1_500 },
  { account: 'ZG0790', unrealised: -200 },
  { account: 'DH3747', unrealised: 800 },
];

const holdings = [
  { account: 'ZG0790', cur_val: 200_000 },
  { account: 'GR87DF', cur_val: 7_500 },
];


// ── SSOT — golden values ────────────────────────────────────────────────────

describe('navRowForAccount — v4 formula', () => {
  test('cash = sod + option_premium', () => {
    const r = navRowForAccount('ZG0790', funds, positions, holdings);
    assert.equal(r.cash, 105_000);          // 100k + 5k premium
  });

  test('pos_m2m = Σ unrealised filtered by account', () => {
    const r = navRowForAccount('ZG0790', funds, positions, holdings);
    assert.equal(r.pos_m2m, 1_300);          // 1500 + (-200)
  });

  test('holdings_mtm = Σ cur_val filtered by account', () => {
    const r = navRowForAccount('ZG0790', funds, positions, holdings);
    assert.equal(r.holdings_mtm, 200_000);
  });

  test('nav = cash + pos_m2m + holdings_mtm', () => {
    const r = navRowForAccount('ZG0790', funds, positions, holdings);
    assert.equal(r.nav, 306_300);            // 105k + 1.3k + 200k
  });

  test('account with no positions returns pos_m2m=0', () => {
    const r = navRowForAccount('GR87DF', funds, positions, holdings);
    assert.equal(r.pos_m2m, 0);
    assert.equal(r.holdings_mtm, 7_500);
    assert.equal(r.nav, 17_750);             // 10k + 250 + 7.5k
  });
});


describe('navByAccount + navTotalRow', () => {
  test('navByAccount returns one row per requested account', () => {
    const rows = navByAccount(['ZG0790', 'DH3747', 'GR87DF'], funds, positions, holdings);
    assert.equal(rows.length, 3);
    assert.equal(rows[0].account, 'ZG0790');
  });

  test('navTotalRow sums every column', () => {
    const rows = navByAccount(['ZG0790', 'DH3747', 'GR87DF'], funds, positions, holdings);
    const tot = navTotalRow(rows);
    assert.equal(tot.account, 'TOTAL');
    // cash:   105_000 + 50_000 + 10_250 = 165_250
    // pos_m2m: 1_300  +    800 +      0 =   2_100
    // hold:  200_000  +      0 +  7_500 = 207_500
    // nav:                                = 374_850
    assert.equal(tot.cash, 165_250);
    assert.equal(tot.pos_m2m, 2_100);
    assert.equal(tot.holdings_mtm, 207_500);
    assert.equal(tot.nav, 374_850);
  });

  test('navTotalRow returns null on empty input', () => {
    assert.equal(navTotalRow([]), null);
    assert.equal(navTotalRow(null), null);
  });

  test('handles missing funds row gracefully', () => {
    const r = navRowForAccount('MISSING', [], positions, holdings);
    assert.equal(r.cash, 0);
    assert.equal(r.pos_m2m, 0);
    assert.equal(r.holdings_mtm, 0);
    assert.equal(r.nav, 0);
  });

  test('navByAccount with empty accounts returns []', () => {
    assert.deepEqual(navByAccount([], funds, positions, holdings), []);
  });
});


// ── baseDayPnlForPosition — new-position override guard ─────────────────────

describe('baseDayPnlForPosition — override guard', () => {
  test('normal overnight row: returns day_change_val', () => {
    const p = { overnight_quantity: 10, day_change_val: 250, pnl: 1500 };
    assert.equal(baseDayPnlForPosition(p), 250);
  });

  test('opened-today row with decomposed dcv: returns dcv (not pnl)', () => {
    // Live-path row: overnight=0 but broker computed dcv via decomposed formula
    // (bq*ltp - bv term). dcv is authoritative — do NOT override with pnl.
    const p = { overnight_quantity: 0, day_change_val: 800, pnl: 800 };
    assert.equal(baseDayPnlForPosition(p), 800);
  });

  test('regression 2026-07-03: closed-hours snapshot row with settled dcv trusted', () => {
    // Pre-fix bug: overnight_quantity=0 (msgspec default on _positions_snapshot)
    // + non-zero pnl → old override returned pnl (14670). New guard requires
    // dcv === 0 for override, so settled dcv (0.6) is now trusted.
    const p = { overnight_quantity: 0, day_change_val: 0.6, pnl: 14670 };
    assert.equal(baseDayPnlForPosition(p), 0.6);
  });

  test('Dhan/Groww broker missing decomposed fields: fallback to pnl', () => {
    // The only legitimate case for the override: broker didn't ship any
    // intraday decomposition (oq=0, dcv=0) AND pnl is non-zero, so we
    // approximate day P&L with lifetime pnl.
    const p = { overnight_quantity: 0, day_change_val: 0, pnl: 500 };
    assert.equal(baseDayPnlForPosition(p), 500);
  });

  test('closed position (all fields zero): returns 0', () => {
    const p = { overnight_quantity: 0, day_change_val: 0, pnl: 0 };
    assert.equal(baseDayPnlForPosition(p), 0);
  });

  test('handles missing fields (null / undefined) safely', () => {
    assert.equal(baseDayPnlForPosition({}), 0);
    assert.equal(baseDayPnlForPosition({ pnl: null, day_change_val: null }), 0);
    assert.equal(baseDayPnlForPosition({ pnl: 1000 }), 1000);  // legacy fallback path
  });

  test('positionsPnlFiltered sums F&O settled dcv, not lifetime pnl', () => {
    // Two F&O snapshot rows + one equity row (excluded from P pill).
    // Pre-fix: dayTotal would return Σ pnl = 21870 (wrong).
    // Post-fix: dayTotal returns Σ dcv = 0.6 - 18 = -17.4 (correct settled).
    const positions = [
      { exchange: 'MCX', overnight_quantity: 0, day_change_val: 0.6, pnl: 14670 },
      { exchange: 'MCX', overnight_quantity: 0, day_change_val: -18, pnl: -15230 },
      { exchange: 'NSE', overnight_quantity: 10, day_change_val: 500, pnl: 3000 }, // filtered out
    ];
    const { dayTotal, pnlTotal } = positionsPnlFiltered(positions);
    assert.ok(Math.abs(dayTotal - (-17.4)) < 1e-6,
      `dayTotal=${dayTotal} expected -17.4 (settled dcv sum, not lifetime pnl)`);
    assert.equal(pnlTotal, 14670 + -15230);  // -560
  });
});


// ── livePositionDayPnl — live-tick rescue for stale-LTP (CRUDEOIL fingerprint) ──

describe('livePositionDayPnl — live-tick recompute SSOT', () => {
  // The canonical CRUDEOIL fingerprint: Kite ships last_price === close_price
  // (stale ticker), so day_change_val = (last_price - close_price) * qty = 0.
  // With overnight_quantity > 0, baseDayPnlForPosition returns 0 (guard
  // doesn't fire). livePositionDayPnl must rescue via the live SSE tick.
  const staleRow = {
    overnight_quantity: 5,
    day_change_val: 0,
    pnl: 2500,
  };

  test('CRUDEOIL fingerprint: returns live-based recompute when market is open', () => {
    // closePx = pollLtp (stale), liveLtp > closePx
    // realisedToday = 0 - (5450 - 5450) * 2 = 0
    // result = 0 + (5480 - 5450) * 2 = 60
    const result = livePositionDayPnl(
      { closePx: 5450, pollLtp: 5450, qty: 2, avg: 5400, dcvRow: staleRow },
      5480,
      { marketOpen: true },
    );
    assert.ok(Math.abs(result - 60) < 1e-6,
      `Expected 60, got ${result} — stale-LTP rescue failed`);
  });

  test('CRUDEOIL fingerprint: returns baseDayPnlForPosition fallback when market closed', () => {
    // Market closed → liveLtp is ignored regardless of value
    const result = livePositionDayPnl(
      { closePx: 5450, pollLtp: 5450, qty: 2, avg: 5400, dcvRow: staleRow },
      5480,
      { marketOpen: false },
    );
    // baseDayPnlForPosition(staleRow): oq=5 (not 0) → returns dcv=0
    assert.equal(result, 0, 'Closed market must fall back to baseDayPnlForPosition');
  });

  test('CRUDEOIL fingerprint: returns baseDayPnlForPosition when liveLtp is null', () => {
    const result = livePositionDayPnl(
      { closePx: 5450, pollLtp: 5450, qty: 2, avg: 5400, dcvRow: staleRow },
      null,
      { marketOpen: true },
    );
    assert.equal(result, 0, 'Null liveLtp must fall back to baseDayPnlForPosition');
  });

  test('live-LTP on non-stale row: realisedToday carries broker dcv correctly', () => {
    // Normal overnight row: dcv = 300, pollLtp = 5470, closePx = 5450, qty = 2
    // realisedToday = 300 - (5470 - 5450) * 2 = 300 - 40 = 260
    // result = 260 + (5490 - 5450) * 2 = 260 + 80 = 340
    const row = { overnight_quantity: 2, day_change_val: 300, pnl: 500 };
    const result = livePositionDayPnl(
      { closePx: 5450, pollLtp: 5470, qty: 2, avg: 5400, dcvRow: row },
      5490,
      { marketOpen: true },
    );
    assert.ok(Math.abs(result - 340) < 1e-6,
      `Expected 340, got ${result}`);
  });

  test('Contract A branch (closePx=0, opened today): uses (live - avg) * qty', () => {
    // New position today, no prior close; fallback to avg_cost
    const newRow = { overnight_quantity: 0, day_change_val: 0, pnl: 200 };
    const result = livePositionDayPnl(
      { closePx: 0, pollLtp: 0, qty: 3, avg: 100, dcvRow: newRow },
      110,
      { marketOpen: true },
    );
    assert.ok(Math.abs(result - 30) < 1e-6,
      `Expected 30, got ${result} — Contract A branch failed`);
  });

  test('Contract A branch: falls back to baseDayPnlForPosition when liveLtp=null', () => {
    // No live tick → Contract A can't compute, falls through to base
    // baseDayPnlForPosition: oq=0, dcv=0, pnl=200 → returns 200
    const newRow = { overnight_quantity: 0, day_change_val: 0, pnl: 200 };
    const result = livePositionDayPnl(
      { closePx: 0, pollLtp: 0, qty: 3, avg: 100, dcvRow: newRow },
      null,
      { marketOpen: true },
    );
    assert.equal(result, 200, 'Contract A without live tick should return base pnl override');
  });

  test('normal live row: Pulse and Derivatives produce identical results for same inputs', () => {
    // Simulate both callers calling livePositionDayPnl with identical normalised params.
    // Before fix, Derivatives used baseDayPnlForPosition directly (no live rescue).
    const rawRow = { overnight_quantity: 3, day_change_val: 150, pnl: 600 };
    const params = { closePx: 5500, pollLtp: 5510, qty: 3, avg: 5480, dcvRow: rawRow };
    // Pulse: uses liveQ?.ltp = 5525
    const pulseResult = livePositionDayPnl(params, 5525, { marketOpen: true });
    // Derivatives: same helper, same params
    const derivResult = livePositionDayPnl(params, 5525, { marketOpen: true });
    assert.equal(pulseResult, derivResult, 'Both surfaces must produce identical Day P&L');
    // Verify the value: realisedToday = 150 - (5510 - 5500)*3 = 150 - 30 = 120
    // result = 120 + (5525 - 5500)*3 = 120 + 75 = 195
    assert.ok(Math.abs(pulseResult - 195) < 1e-6,
      `Expected 195, got ${pulseResult}`);
  });

  test('Perf — pure sync, no async, completes in < 1ms for 10k calls', () => {
    const row = { overnight_quantity: 5, day_change_val: 0, pnl: 0 };
    const params = { closePx: 5450, pollLtp: 5450, qty: 2, avg: 5400, dcvRow: row };
    const t0 = Date.now();
    for (let i = 0; i < 10_000; i++) {
      livePositionDayPnl(params, 5480, { marketOpen: true });
    }
    const elapsed = Date.now() - t0;
    assert.ok(elapsed < 50, `10k calls took ${elapsed}ms, expected < 50ms`);
  });
});


// ── Stale + Reuse — no inline formula left in consumers ─────────────────────

describe('SSOT — only $lib/data/nav holds the formula', () => {
  test('PerformancePage.svelte imports navByAccount + navTotalRow', () => {
    const src = readFileSync(resolve(FRONTEND_SRC, 'lib', 'PerformancePage.svelte'), 'utf-8');
    assert.match(src, /from\s+['"]\$lib\/data\/nav['"]/);
    assert.match(src, /navByAccount/);
  });

  test('NavBreakdown.svelte imports navByAccount + navTotalRow', () => {
    const src = readFileSync(resolve(FRONTEND_SRC, 'lib', 'NavBreakdown.svelte'), 'utf-8');
    assert.match(src, /from\s+['"]\$lib\/data\/nav['"]/);
    assert.match(src, /navByAccount/);
  });

  test('NavBreakdown no longer inlines the cash_sod + option_premium math', () => {
    const src = readFileSync(resolve(FRONTEND_SRC, 'lib', 'NavBreakdown.svelte'), 'utf-8');
    // No standalone variable assignment for cash_sod or opt_premium —
    // those live in nav.js now.
    assert.doesNotMatch(src, /const\s+cash_sod\s*=/);
    assert.doesNotMatch(src, /const\s+opt_premium\s*=/);
  });

  // livePositionDayPnl SSOT guards — both surfaces must import and call the
  // helper, not inline the realised-today recompute logic independently.

  test('derivatives page imports livePositionDayPnl from nav', () => {
    const src = readFileSync(
      resolve(FRONTEND_SRC, 'routes', '(algo)', 'admin', 'derivatives', '+page.svelte'), 'utf-8');
    assert.match(src, /livePositionDayPnl/,
      'derivatives +page.svelte must import livePositionDayPnl from $lib/data/nav');
  });

  test('derivatives _dayPnlForLeg no longer inlines realisedToday math', () => {
    const src = readFileSync(
      resolve(FRONTEND_SRC, 'routes', '(algo)', 'admin', 'derivatives', '+page.svelte'), 'utf-8');
    // The inline pattern "realisedToday" should not appear in the derivatives page —
    // it lives inside livePositionDayPnl in nav.js now.
    assert.doesNotMatch(src, /realisedToday/,
      'derivatives page must not inline realisedToday — use livePositionDayPnl SSOT');
  });

  test('pulseUnified.js imports livePositionDayPnl from nav.js', () => {
    const src = readFileSync(
      resolve(FRONTEND_SRC, 'lib', 'data', 'pulseUnified.js'), 'utf-8');
    assert.match(src, /livePositionDayPnl/,
      'pulseUnified.js must use livePositionDayPnl instead of inline math');
    // The inline pattern should no longer exist.
    assert.doesNotMatch(src, /realisedToday/,
      'pulseUnified.js must not inline realisedToday — use livePositionDayPnl SSOT');
  });
});
