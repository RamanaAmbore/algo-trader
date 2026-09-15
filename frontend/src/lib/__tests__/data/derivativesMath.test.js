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
import { annotateOptionCandidates, rawPosExpPnl } from '$lib/data/derivativesMath.js';

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
// rawPosExpPnl — compute expiry-day P&L for positions
// ─────────────────────────────────────────────────────────────────────────────

describe('rawPosExpPnl', () => {
  /**
   * Test 1: CE option with valid spot above strike
   * c = { tradingsymbol: 'NIFTY25SEPC24000CE', quantity: 50, average_price: 200, realised: 0, pnl: 0, kind: 'opt' }
   * spot = 24500
   * intrinsic = max(0, 24500 - 24000) = 500
   * Expected: (500 - 200) * 50 + 0 = 15000
   */
  it('CE option with valid spot above strike', () => {
    const c = {
      tradingsymbol: 'NIFTY25SEPC24000CE',
      quantity: 50,
      average_price: 200,
      realised: 0,
      pnl: 0,
      kind: 'opt',
    };
    const spot = 24500;
    const result = rawPosExpPnl(c, spot, {});
    expect(result).toBeCloseTo(15000, 2);
  });

  /**
   * Test 2: PE option short (negative qty)
   * c = { tradingsymbol: 'NIFTY25SEPC24000PE', quantity: -50, average_price: 150, realised: 500, pnl: 0, kind: 'opt' }
   * spot = 23500 (put is in the money: intrinsic = 500)
   * intrinsic = max(0, 24000 - 23500) = 500
   * Expected: (500 - 150) * -50 + 500 = -17000
   */
  it('PE option short (negative qty)', () => {
    const c = {
      tradingsymbol: 'NIFTY25SEPC24000PE',
      quantity: -50,
      average_price: 150,
      realised: 500,
      pnl: 0,
      kind: 'opt',
    };
    const spot = 23500;
    const result = rawPosExpPnl(c, spot, {});
    expect(result).toBeCloseTo(-17000, 2);
  });

  /**
   * Test 3: Future with valid spot parameter (preferred over last_price)
   * c = { tradingsymbol: 'CRUDEOIL26AUGFUT', quantity: 10, average_price: 7300, last_price: 7600, realised: 0, pnl: 0, kind: 'fut' }
   * spot = 7580 (spot is preferred)
   * Expected: (7580 - 7300) * 10 + 0 = 2800
   */
  it('Future with valid spot parameter overrides last_price', () => {
    const c = {
      tradingsymbol: 'CRUDEOIL26AUGFUT',
      quantity: 10,
      average_price: 7300,
      last_price: 7600,
      realised: 0,
      pnl: 0,
      kind: 'fut',
    };
    const spot = 7580;
    const result = rawPosExpPnl(c, spot, {});
    expect(result).toBeCloseTo(2800, 2);
  });

  /**
   * Test 4: Future with spot=0 falls back to last_price
   * c = { tradingsymbol: 'CRUDEOIL26AUGFUT', quantity: 10, average_price: 7300, last_price: 7580, realised: 0, pnl: 0, kind: 'fut' }
   * spot = 0 (invalid, should fall back to last_price)
   * Expected: (7580 - 7300) * 10 + 0 = 2800
   */
  it('Future with spot=0 falls back to last_price', () => {
    const c = {
      tradingsymbol: 'CRUDEOIL26AUGFUT',
      quantity: 10,
      average_price: 7300,
      last_price: 7580,
      realised: 0,
      pnl: 0,
      kind: 'fut',
    };
    const spot = 0;
    const result = rawPosExpPnl(c, spot, {});
    expect(result).toBeCloseTo(2800, 2);
  });

  /**
   * Test 5: Future with spot=null falls back to last_price
   * c = { tradingsymbol: 'CRUDEOIL26AUGFUT', quantity: 10, average_price: 7300, last_price: 7580, realised: 0, pnl: 0, kind: 'fut' }
   * spot = null (invalid, should fall back to last_price)
   * Expected: (7580 - 7300) * 10 + 0 = 2800
   */
  it('Future with spot=null falls back to last_price', () => {
    const c = {
      tradingsymbol: 'CRUDEOIL26AUGFUT',
      quantity: 10,
      average_price: 7300,
      last_price: 7580,
      realised: 0,
      pnl: 0,
      kind: 'fut',
    };
    const spot = null;
    const result = rawPosExpPnl(c, spot, {});
    expect(result).toBeCloseTo(2800, 2);
  });

  /**
   * Test 6: Future with spot=0 and last_price=0 returns null
   * c = { tradingsymbol: 'CRUDEOIL26AUGFUT', quantity: 10, average_price: 7300, last_price: 0, realised: 0, pnl: 0, kind: 'fut' }
   * spot = 0 (both invalid)
   * Expected: null
   */
  it('Future with spot=0 and last_price=0 returns null', () => {
    const c = {
      tradingsymbol: 'CRUDEOIL26AUGFUT',
      quantity: 10,
      average_price: 7300,
      last_price: 0,
      realised: 0,
      pnl: 0,
      kind: 'fut',
    };
    const spot = 0;
    const result = rawPosExpPnl(c, spot, {});
    expect(result).toBeNull();
  });

  /**
   * Test 7: Closed leg (qty=0)
   * c = { tradingsymbol: 'NIFTY25SEPC24000CE', quantity: 0, average_price: 200, realised: 5000, pnl: 0, kind: 'opt' }
   * spot = 24500
   * Expected: 5000 (realised || pnl)
   */
  it('Closed leg (qty=0) returns realised', () => {
    const c = {
      tradingsymbol: 'NIFTY25SEPC24000CE',
      quantity: 0,
      average_price: 200,
      realised: 5000,
      pnl: 0,
      kind: 'opt',
    };
    const spot = 24500;
    const result = rawPosExpPnl(c, spot, {});
    expect(result).toBe(5000);
  });

  /**
   * Test 8: Option with spot=0
   * c = { tradingsymbol: 'NIFTY25SEPC24000CE', quantity: 50, average_price: 200, realised: 0, pnl: 0, kind: 'opt' }
   * spot = 0
   * Expected: null (spot <= 0)
   */
  it('Option with spot=0 returns null', () => {
    const c = {
      tradingsymbol: 'NIFTY25SEPC24000CE',
      quantity: 50,
      average_price: 200,
      realised: 0,
      pnl: 0,
      kind: 'opt',
    };
    const spot = 0;
    const result = rawPosExpPnl(c, spot, {});
    expect(result).toBeNull();
  });

  /**
   * Test 9: Option with spot=null
   * c = { tradingsymbol: 'NIFTY25SEPC24000CE', quantity: 50, average_price: 200, realised: 0, pnl: 0, kind: 'opt' }
   * spot = null
   * Expected: null (spot == null)
   */
  it('Option with spot=null returns null', () => {
    const c = {
      tradingsymbol: 'NIFTY25SEPC24000CE',
      quantity: 50,
      average_price: 200,
      realised: 0,
      pnl: 0,
      kind: 'opt',
    };
    const spot = null;
    const result = rawPosExpPnl(c, spot, {});
    expect(result).toBeNull();
  });

  /**
   * Test 10: Uses legAnalytics strike when provided (skips regex parse)
   * c = { tradingsymbol: 'SOMEWEIRDOPTION', quantity: 75, average_price: 100, realised: 0, pnl: 0, kind: 'opt' }
   * legAnalytics = { 'SOMEWEIRDOPTION': { strike: 23000, opt_type: 'CE' } }
   * spot = 23500
   * intrinsic = max(0, 23500 - 23000) = 500
   * Expected: (500 - 100) * 75 + 0 = 30000
   */
  it('Uses legAnalytics strike when provided (skips regex parse)', () => {
    const c = {
      tradingsymbol: 'SOMEWEIRDOPTION',
      quantity: 75,
      average_price: 100,
      realised: 0,
      pnl: 0,
      kind: 'opt',
    };
    const spot = 23500;
    const legAnalytics = {
      SOMEWEIRDOPTION: { strike: 23000, opt_type: 'CE' },
    };
    const result = rawPosExpPnl(c, spot, legAnalytics);
    expect(result).toBeCloseTo(30000, 2);
  });
});
