/**
 * derivativesMath.test.js — Vitest tests for annotateOptionCandidates and
 * related helpers in derivativesMath.js.
 *
 * Five quality dimensions:
 *  1. SSOT  — exercises the same module path used by +page.svelte
 *  2. Perf  — all synchronous; no I/O
 *  3. Stale — guards against regressions in the qty=0 / expFilter guard
 *  4. Reuse — same helpers used by the derivatives expiry-close analysis
 *  5. UX    — zero-qty (closed) positions must not appear in expiry bands
 */

import { describe, it, expect } from 'vitest';
import { annotateOptionCandidates, rollupByUnderlying, perRootReduce, buildStrategyMatcher, interpAt } from '$lib/data/derivativesMath.js';
import { legExtrinsicDisplay } from '$lib/data/expiryPnl.js';

// ─────────────────────────────────────────────────────────────────────────────
// Minimal fixture helpers
// ─────────────────────────────────────────────────────────────────────────────

/** Build a minimal instrument record for a CE/PE option. */
function makeInst(optType, strike, underlying, expiry = '2026-08-28') {
  return { t: optType, k: strike, u: underlying, x: expiry };
}

/** Build a minimal candidate row. */
function makeCand(sym, qty, extra = {}) {
  return { symbol: sym, qty, account: 'ZG0790', source: 'live', ...extra };
}

const SPOT_800 = 800;
const MCX_EMPTY = new Set();

// ─────────────────────────────────────────────────────────────────────────────
// annotateOptionCandidates — qty=0 guard
// ─────────────────────────────────────────────────────────────────────────────

describe('annotateOptionCandidates — qty=0 guard', () => {
  it('skips qty=0 rows when expFilter is empty', () => {
    const getInstrument = (sym) =>
      sym === 'NIFTY26AUG800CE' ? makeInst('CE', 800, 'NIFTY') : null;
    const candidates = [makeCand('NIFTY26AUG800CE', 0)];
    const result = annotateOptionCandidates({
      candidates,
      spot: SPOT_800,
      expFilter: [],
      mcxUnderlyings: MCX_EMPTY,
      legAnalytics: {},
      getInstrument,
    });
    expect(result).toHaveLength(0);
  });

  it('skips qty=0 rows even when expFilter is non-empty (regression: Bug 2)', () => {
    // Before the fix, `qty === 0 && !expFilter.length` would pass qty=0 rows
    // through when expFilter was set (e.g. ['2026-08-28']).
    const getInstrument = (sym) =>
      sym === 'NIFTY26AUG800CE' ? makeInst('CE', 800, 'NIFTY') : null;
    const candidates = [makeCand('NIFTY26AUG800CE', 0)];
    const result = annotateOptionCandidates({
      candidates,
      spot: SPOT_800,
      expFilter: ['2026-08-28'],
      mcxUnderlyings: MCX_EMPTY,
      legAnalytics: {},
      getInstrument,
    });
    expect(result).toHaveLength(0);
  });

  it('includes non-zero qty rows when expFilter is non-empty', () => {
    const getInstrument = (sym) =>
      sym === 'NIFTY26AUG800CE' ? makeInst('CE', 800, 'NIFTY') : null;
    const candidates = [makeCand('NIFTY26AUG800CE', 50)];
    const result = annotateOptionCandidates({
      candidates,
      spot: SPOT_800,
      expFilter: ['2026-08-28'],
      mcxUnderlyings: MCX_EMPTY,
      legAnalytics: {},
      getInstrument,
    });
    expect(result).toHaveLength(1);
    expect(result[0]._qty).toBe(50);
  });

  it('includes non-zero qty rows when expFilter is empty', () => {
    const getInstrument = (sym) =>
      sym === 'NIFTY26AUG800PE' ? makeInst('PE', 800, 'NIFTY') : null;
    const candidates = [makeCand('NIFTY26AUG800PE', -50)];
    const result = annotateOptionCandidates({
      candidates,
      spot: SPOT_800,
      expFilter: [],
      mcxUnderlyings: MCX_EMPTY,
      legAnalytics: {},
      getInstrument,
    });
    expect(result).toHaveLength(1);
    expect(result[0]._qty).toBe(-50);
  });

  it('skips draft-source rows regardless of qty', () => {
    const getInstrument = (sym) =>
      sym === 'NIFTY26AUG800CE' ? makeInst('CE', 800, 'NIFTY') : null;
    const candidates = [makeCand('NIFTY26AUG800CE', 50, { source: 'draft' })];
    const result = annotateOptionCandidates({
      candidates,
      spot: SPOT_800,
      expFilter: ['2026-08-28'],
      mcxUnderlyings: MCX_EMPTY,
      legAnalytics: {},
      getInstrument,
    });
    expect(result).toHaveLength(0);
  });

  it('skips rows where getInstrument returns null', () => {
    const candidates = [makeCand('UNKNOWN24AUG800CE', 50)];
    const result = annotateOptionCandidates({
      candidates,
      spot: SPOT_800,
      expFilter: ['2026-08-28'],
      mcxUnderlyings: MCX_EMPTY,
      legAnalytics: {},
      getInstrument: () => null,
    });
    expect(result).toHaveLength(0);
  });

  it('correctly mixes zero and non-zero qty rows — only non-zero passes', () => {
    const getInstrument = (sym) => {
      if (sym === 'NIFTY26AUG800CE') return makeInst('CE', 800, 'NIFTY');
      if (sym === 'NIFTY26AUG750PE') return makeInst('PE', 750, 'NIFTY');
      return null;
    };
    const candidates = [
      makeCand('NIFTY26AUG800CE', 0),   // closed — should be skipped
      makeCand('NIFTY26AUG750PE', -50), // open short — should pass
    ];
    const result = annotateOptionCandidates({
      candidates,
      spot: SPOT_800,
      expFilter: ['2026-08-28'],
      mcxUnderlyings: MCX_EMPTY,
      legAnalytics: {},
      getInstrument,
    });
    expect(result).toHaveLength(1);
    expect(result[0].symbol).toBe('NIFTY26AUG750PE');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// rollupByUnderlying — holdingsDayPnlByKey param
// ─────────────────────────────────────────────────────────────────────────────

describe('rollupByUnderlying — holdingsDayPnlByKey param', () => {
  /**
   * Build a minimal position row for rollupByUnderlying input.
   * Symbols should be F&O (futures/options) with format UNDERLYING+EXPIRY+TYPE.
   * @param {string} sym  F&O symbol (e.g., 'NIFTY25SEPCFUT', 'TCS25SEPCFUT')
   * @param {number} qty
   * @param {Partial<any>} extra
   * @returns {any}
   */
  function makePos(sym, qty, extra = {}) {
    return {
      symbol: sym,
      tradingsymbol: sym,
      quantity: qty,
      pnl: 500,
      source: 'live',
      account: 'ACC1',
      ...extra,
    };
  }

  /**
   * Build a minimal holding row for rollupByUnderlying input.
   * @param {string} sym
   * @param {number} qty
   * @param {Partial<any>} extra
   * @returns {any}
   */
  function makeHold(sym, qty, extra = {}) {
    return {
      symbol: sym,
      tradingsymbol: sym,
      quantity: qty,
      opening_quantity: qty,
      pnl: 1000,
      day_change_val: 100,
      account: 'ACC1',
      ...extra,
    };
  }

  /**
   * Mock baseDayPnlForPosition that just returns a day_pnl from the position.
   * @param {any} p
   * @returns {number}
   */
  function mockBaseDayPnlForPosition(p) {
    return Number(p.day_change_val ?? p.day_pnl ?? 0);
  }

  it('uses holdingsDayPnlByKey[sym] when present, overrides day_change_val', () => {
    // Holdings row with day_change_val: 100, but holdingsDayPnlByKey['TCS']: 250
    // Expected: day_with in rollup uses 250 (from override), not 100.
    const holdings = [makeHold('TCS', 100, { day_change_val: 100 })];
    const positions = [makePos('TCS25SEPCFUT', 50)]; // TCS future to satisfy legs_without > 0 for TCS root
    const result = rollupByUnderlying({
      positions,
      holdings,
      wantedSource: 'live',
      matchAccount: () => true,
      matchStrategy: () => true,
      filterQ: '',
      decomposeSymbol: (sym) => {
        const m = /^([A-Z&]+?)\d+/.exec(sym);
        return { root: m ? m[1] : sym };
      },
      targetsForProxy: (sym) => [],
      getOptionUnderlyingLot: (root) => (root === 'TCS' ? 1 : 0),
      baseDayPnlForPosition: mockBaseDayPnlForPosition,
      holdingsDayPnlByKey: { TCS: 250 }, // Override 100 with 250
    });

    const tcsGroup = result.find((g) => g.underlying === 'TCS');
    expect(tcsGroup).toBeDefined();
    expect(tcsGroup.day_with).toBe(250);
    expect(tcsGroup.day_with).not.toBe(100);
  });

  it('falls back to day_change_val when holdingsDayPnlByKey missing the symbol', () => {
    // Holdings row with day_change_val: 200. holdingsDayPnlByKey: {} (empty)
    // Expected: day_with uses 200 (fallback).
    const holdings = [makeHold('INFY', 50, { day_change_val: 200 })];
    const positions = [makePos('INFY25SEPCFUT', 50)]; // INFY future
    const result = rollupByUnderlying({
      positions,
      holdings,
      wantedSource: 'live',
      matchAccount: () => true,
      matchStrategy: () => true,
      filterQ: '',
      decomposeSymbol: (sym) => {
        const m = /^([A-Z&]+?)\d+/.exec(sym);
        return { root: m ? m[1] : sym };
      },
      targetsForProxy: (sym) => [],
      getOptionUnderlyingLot: (root) => (root === 'INFY' ? 1 : 0),
      baseDayPnlForPosition: mockBaseDayPnlForPosition,
      holdingsDayPnlByKey: {}, // Empty map — fallback to day_change_val
    });

    const infyGroup = result.find((g) => g.underlying === 'INFY');
    expect(infyGroup).toBeDefined();
    expect(infyGroup.day_with).toBe(200);
  });

  it('defaults to empty holdingsDayPnlByKey when param is omitted', () => {
    // Same as above, but param is not passed — should still use day_change_val.
    const holdings = [makeHold('RELIANCE', 75, { day_change_val: 300 })];
    const positions = [makePos('RELIANCE25SEPCFUT', 50)];
    const result = rollupByUnderlying({
      positions,
      holdings,
      wantedSource: 'live',
      matchAccount: () => true,
      matchStrategy: () => true,
      filterQ: '',
      decomposeSymbol: (sym) => {
        const m = /^([A-Z&]+?)\d+/.exec(sym);
        return { root: m ? m[1] : sym };
      },
      targetsForProxy: (sym) => [],
      getOptionUnderlyingLot: (root) => (root === 'RELIANCE' ? 1 : 0),
      baseDayPnlForPosition: mockBaseDayPnlForPosition,
      // holdingsDayPnlByKey is omitted
    });

    const relianceGroup = result.find((g) => g.underlying === 'RELIANCE');
    expect(relianceGroup).toBeDefined();
    expect(relianceGroup.day_with).toBe(300);
  });

  it('holdingsDayPnlByKey = 0 for a symbol: uses 0, not day_change_val', () => {
    // Holdings row with day_change_val: 500, but holdingsDayPnlByKey['HCLTECH']: 0
    // Nullish coalescing: 0 ?? 500 → 0 (0 is not null/undefined)
    // Expected: day_with uses 0.
    const holdings = [makeHold('HCLTECH', 60, { day_change_val: 500 })];
    const positions = [makePos('HCLTECH25SEPCFUT', 50)];
    const result = rollupByUnderlying({
      positions,
      holdings,
      wantedSource: 'live',
      matchAccount: () => true,
      matchStrategy: () => true,
      filterQ: '',
      decomposeSymbol: (sym) => {
        const m = /^([A-Z&]+?)\d+/.exec(sym);
        return { root: m ? m[1] : sym };
      },
      targetsForProxy: (sym) => [],
      getOptionUnderlyingLot: (root) => (root === 'HCLTECH' ? 1 : 0),
      baseDayPnlForPosition: mockBaseDayPnlForPosition,
      holdingsDayPnlByKey: { HCLTECH: 0 }, // Override with 0
    });

    const hlcGroup = result.find((g) => g.underlying === 'HCLTECH');
    expect(hlcGroup).toBeDefined();
    expect(hlcGroup.day_with).toBe(0);
    expect(hlcGroup.day_with).not.toBe(500);
  });

  it('multiple holdings with mixed override/fallback', () => {
    // TCS gets override (250), INFY gets fallback (200), RELIANCE gets fallback (300).
    const holdings = [
      makeHold('TCS', 100, { day_change_val: 100 }),
      makeHold('INFY', 50, { day_change_val: 200 }),
      makeHold('RELIANCE', 75, { day_change_val: 300 }),
    ];
    const positions = [
      makePos('TCS25SEPCFUT', 50),
      makePos('INFY25SEPCFUT', 50),
      makePos('RELIANCE25SEPCFUT', 50),
    ];
    const result = rollupByUnderlying({
      positions,
      holdings,
      wantedSource: 'live',
      matchAccount: () => true,
      matchStrategy: () => true,
      filterQ: '',
      decomposeSymbol: (sym) => {
        const m = /^([A-Z&]+?)\d+/.exec(sym);
        return { root: m ? m[1] : sym };
      },
      targetsForProxy: (sym) => [],
      getOptionUnderlyingLot: (root) => {
        if (root === 'TCS') return 1;
        if (root === 'INFY') return 1;
        if (root === 'RELIANCE') return 1;
        return 0;
      },
      baseDayPnlForPosition: mockBaseDayPnlForPosition,
      holdingsDayPnlByKey: { TCS: 250 }, // Only TCS overridden
    });

    const tcsGroup = result.find((g) => g.underlying === 'TCS');
    const infyGroup = result.find((g) => g.underlying === 'INFY');
    const relianceGroup = result.find((g) => g.underlying === 'RELIANCE');

    expect(tcsGroup.day_with).toBe(250);
    expect(infyGroup.day_with).toBe(200);
    expect(relianceGroup.day_with).toBe(300);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// perRootReduce — item-2 fix (round 4): per-row accessor, no double-counting
// across accounts holding the same symbol. Confirmed worked example:
// CRUDEOIL CE held in ACCT_A and ACCT_B, real per-account extrinsic −3,000
// each (real cross-account total −6,000, per-account share −3,000).
// ─────────────────────────────────────────────────────────────────────────────

describe('perRootReduce — Extrinsic, multi-account same-symbol (item-2 fix)', () => {
  // strike 6000, pollAnchor (underlying poll-time spot) 6100 → intrinsic 100.
  // ltp (option's own poll price) 130, qty 100 per account → extrinsic
  // (intrinsic - ltp) * qty = (100 - 130) * 100 = -3000 per row.
  const rowFor = (account) => ({
    symbol: 'CRUDEOIL6000CE', account, source: 'live',
    qty: 100, avg_cost: 125, ltp: 130, underlying_ltp: 6100,
  });
  const positions = [rowFor('ACCT_A'), rowFor('ACCT_B')];

  const reduceParams = (matchAccount) => ({
    positions,
    wantedSource: /** @type {'live'} */ ('live'),
    matchAccount,
    matchStrategy: () => true,
    decomposeSymbol: () => ({ root: 'CRUDEOIL' }),
    getSpot: () => 0, // unused — legExtrinsicDisplay reads c.underlying_ltp, not the getSpot-resolved value
    accessor: (c) => legExtrinsicDisplay(c, Number(c?.underlying_ltp) || 0),
  });

  it('sums real per-account contributions across both accounts: -3000 + -3000 = -6000 (NOT -12000)', () => {
    const out = perRootReduce(reduceParams(() => true));
    expect(out.CRUDEOIL).toBe(-6000);
  });

  it('filtered to ACCT_A alone: shows that account\'s own share (-3000), not the whole-book cross-account sum', () => {
    const out = perRootReduce(reduceParams((a) => a === 'ACCT_A'));
    expect(out.CRUDEOIL).toBe(-3000);
  });

  it('round-trip against the pre-fix bug: an accessor that returns a value ALREADY pre-summed across every account (simulating positionsDerivedStore.get(sym).extrinsic, keyed by symbol only) double-counts when perRootReduce walks per-account rows', () => {
    // This reproduces the exact round-3 defect: positionsDerivedStore's
    // byKey/byRoot maps are pre-summed across every account for a given
    // symbol — indexing that pre-summed value from INSIDE a per-account
    // row walk adds the same total once per account row touched.
    const preSummedCrossAccountTotal = -6000; // what the store would report for this symbol
    const oldBuggyAccessor = () => preSummedCrossAccountTotal;
    const out = perRootReduce({ ...reduceParams(() => true), accessor: oldBuggyAccessor });
    // Confirms the bug: sums to -12000 (2x the real -6000) when the
    // accessor doesn't compute per-row — proving this suite WOULD have
    // caught the round-3 regression had it existed at the time.
    expect(out.CRUDEOIL).toBe(-12000);
    expect(out.CRUDEOIL).not.toBe(-6000);
  });

  it('strategy filter (buildStrategyMatcher): excludes a symbol not in the strategy\'s open legs, matching the Exp P&L filter basis (item-3)', () => {
    const matchStrategy = buildStrategyMatcher('strat-1', new Set(['CRUDEOIL6000CE']));
    const outIncluded = perRootReduce({ ...reduceParams(() => true), matchStrategy });
    expect(outIncluded.CRUDEOIL).toBe(-6000);

    const matchStrategyExcluding = buildStrategyMatcher('strat-1', new Set(['CRUDEOIL6500PE']));
    const outExcluded = perRootReduce({ ...reduceParams(() => true), matchStrategy: matchStrategyExcluding });
    expect(outExcluded.CRUDEOIL).toBeUndefined();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// interpAt — payoff-curve linear interpolation (C4 fix)
// ─────────────────────────────────────────────────────────────────────────────
describe('interpAt', () => {
  const grid = [
    { spot: 100, today_value: -500, expiry_value: -1000 },
    { spot: 110, today_value: 0,    expiry_value: 0 },
    { spot: 120, today_value: 500,  expiry_value: 1000 },
  ];

  it('interpolates linearly between two bracketing grid points (not nearest-point snap)', () => {
    // Nearest-point would snap 105 -> either 100 or 110 exactly; interpolation
    // must land exactly halfway: (-500 + 0) / 2 = -250.
    expect(interpAt(grid, 105, 'today_value')).toBe(-250);
    expect(interpAt(grid, 105, 'expiry_value')).toBe(-500);
  });

  it('returns the exact grid value when x lands exactly on a grid point', () => {
    expect(interpAt(grid, 110, 'today_value')).toBe(0);
    expect(interpAt(grid, 120, 'expiry_value')).toBe(1000);
  });

  it('clamps to the nearest edge value rather than extrapolating out-of-range', () => {
    expect(interpAt(grid, 50, 'today_value')).toBe(-500);
    expect(interpAt(grid, 500, 'today_value')).toBe(500);
  });

  it('returns null when either bracketing point has a null value (client stub today_value before BS pricing lands)', () => {
    const stubGrid = [
      { spot: 100, today_value: null, expiry_value: -1000 },
      { spot: 110, today_value: null, expiry_value: 0 },
    ];
    expect(interpAt(stubGrid, 105, 'today_value')).toBeNull();
    expect(interpAt(stubGrid, 105, 'expiry_value')).toBe(-500);
  });

  it('returns null for an empty array or a non-finite x', () => {
    expect(interpAt([], 100, 'today_value')).toBeNull();
    expect(interpAt(grid, null, 'today_value')).toBeNull();
    expect(interpAt(grid, NaN, 'today_value')).toBeNull();
  });

  it('handles a single-point array without dividing by zero', () => {
    expect(interpAt([{ spot: 100, today_value: 42, expiry_value: 7 }], 100, 'today_value')).toBe(42);
    expect(interpAt([{ spot: 100, today_value: 42, expiry_value: 7 }], 999, 'today_value')).toBe(42);
  });

  it('matches nearest-point behaviour exactly ON a grid point (regression guard vs the old snap logic)', () => {
    // At x==grid[i].spot exactly, both interpolation and nearest-point agree —
    // this is the case that masked C4's bug for NSE before the C2 live-tick fix
    // (payoffSpot used to equal a grid point exactly).
    for (const p of grid) {
      expect(interpAt(grid, p.spot, 'today_value')).toBe(p.today_value);
    }
  });
});
