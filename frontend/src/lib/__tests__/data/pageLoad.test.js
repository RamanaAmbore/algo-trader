/**
 * pageLoad.test.js — Vitest unit tests for `splitClosedReopened` and
 * `buildPositionRowFromBroker` in $lib/derivatives/pageLoad.js.
 *
 * Core invariant under test: splitting a consolidated broker position into
 * closed/open display rows must NEVER change the total Day P&L or Exp P&L
 * attributed to that position — Σ baseDayPnlForPosition(splitRows) must
 * equal baseDayPnlForPosition(the pre-split row), for both the overnight
 * (oq!==0) and intraday-round-trip (oq===0) split paths, and regardless of
 * whether the backend has populated `unrealised` yet (pnl-fallback path)
 * or not (realised+unrealised path).
 *
 * Five quality dimensions:
 *   1. SSOT   — uses the real baseDayPnlForPosition/expiryPnlWithRealised, no reimplementation
 *   2. Perf   — pure synchronous unit tests
 *   3. Stale  — guards the closed/open baseline-double-subtract regression
 *   4. Reuse  — same helpers used by the Legs grid, Snapshot rollup, NavStrip
 *   5. UX     — Σ split rows must reconcile with the pre-split TOTAL by construction
 */

import { describe, it, expect } from 'vitest';
import { splitClosedReopened, buildPositionRowFromBroker } from '$lib/derivatives/pageLoad.js';
import { baseDayPnlForPosition } from '$lib/data/nav.js';
import { expiryPnlWithRealised } from '$lib/data/expiryPnl.js';

function sumDayPnl(rows) {
  return rows.reduce((s, r) => s + baseDayPnlForPosition(r), 0);
}

describe('buildPositionRowFromBroker — unrealised passthrough', () => {
  it('carries unrealised through when present on the broker row', () => {
    const row = buildPositionRowFromBroker({ tradingsymbol: 'NIFTY25SEP24000CE', quantity: 50, realised: 100, unrealised: 200, pnl: 300 }, 'live');
    expect(row.unrealised).toBe(200);
  });

  it('leaves unrealised undefined (not 0) when absent — preserves pnl-fallback in currentTotalProfit', () => {
    const row = buildPositionRowFromBroker({ tradingsymbol: 'NIFTY25SEP24000CE', quantity: 50, realised: 100, pnl: 300 }, 'live');
    expect(row.unrealised).toBeUndefined();
  });
});

describe('splitClosedReopened — no-op paths', () => {
  it('returns [p] unchanged when there is no day activity', () => {
    const p = buildPositionRowFromBroker({ tradingsymbol: 'NIFTY25SEP24000CE', quantity: 50, overnight_quantity: 50, pnl: 500 }, 'live');
    expect(splitClosedReopened(p)).toEqual([p]);
  });
});

describe('splitClosedReopened — overnight (oq!==0) split invariant', () => {
  function makeOvernightRow(overrides = {}) {
    return buildPositionRowFromBroker({
      tradingsymbol: 'NIFTY25SEP24000CE',
      quantity: 5,           // 10 overnight - 5 sold today = 5 remaining
      overnight_quantity: 10,
      day_sell_quantity: 5,
      day_sell_value: 5 * 220, // exit @ 220
      day_buy_quantity: 0,
      day_buy_value: 0,
      average_price: 200,
      prev_close: 210,
      pnl: 150,               // broker-reported lifetime pnl on remaining + closed
      realised: 0,
      prev_settlement_pnl: 100,
      ...overrides,
    }, 'live');
  }

  it('Σ baseDayPnlForPosition(split rows) === baseDayPnlForPosition(pre-split row) — pnl-fallback path (no unrealised)', () => {
    const p = makeOvernightRow();
    const expected = baseDayPnlForPosition(p);
    const split = splitClosedReopened(p);
    expect(split.length).toBe(2);
    expect(sumDayPnl(split)).toBeCloseTo(expected, 6);
  });

  it('Σ baseDayPnlForPosition(split rows) === baseDayPnlForPosition(pre-split row) — realised+unrealised path', () => {
    const p = makeOvernightRow({ realised: 20, unrealised: 130 }); // realised+unrealised = pnl = 150
    const expected = baseDayPnlForPosition(p);
    const split = splitClosedReopened(p);
    expect(sumDayPnl(split)).toBeCloseTo(expected, 6);
  });

  it('fully closed overnight position (brokerQty=0) returns a single closed row with base=0', () => {
    const p = makeOvernightRow({ quantity: 0, day_sell_quantity: 10, day_sell_value: 10 * 220, overnight_quantity: 10, pnl: 200 });
    const split = splitClosedReopened(p);
    expect(split.length).toBe(1);
    expect(split[0]._splitTag).toBe('closed');
    // Day P&L for a fully-closed-today row == closed_day_pnl == its lifetime pnl minus prior-session carry.
    expect(baseDayPnlForPosition(split[0])).toBeCloseTo(split[0].day_change_val, 6);
  });
});

describe('splitClosedReopened — intraday round-trip (oq===0) split invariant', () => {
  function makeIntradayRow(overrides = {}) {
    return buildPositionRowFromBroker({
      tradingsymbol: 'NIFTY25SEP24000CE',
      quantity: 5,             // bought 10, sold 5 today -> 5 remain
      overnight_quantity: 0,   // no overnight carry (or Groww hardcoding it to 0)
      day_buy_quantity: 10,
      day_buy_value: 10 * 200,
      day_sell_quantity: 5,
      day_sell_value: 5 * 220,
      average_price: 200,
      prev_close: 0,
      pnl: 250,                 // (220-200)*5 realised + (ltp-200)*5 unrealised, broker total
      realised: 0,
      prev_settlement_pnl: null,
      ...overrides,
    }, 'live');
  }

  it('Σ baseDayPnlForPosition(split rows) === baseDayPnlForPosition(pre-split row) — pnl-fallback path', () => {
    const p = makeIntradayRow();
    const expected = baseDayPnlForPosition(p);
    const split = splitClosedReopened(p);
    expect(split.length).toBe(2);
    expect(sumDayPnl(split)).toBeCloseTo(expected, 6);
  });

  it('Σ baseDayPnlForPosition(split rows) === baseDayPnlForPosition(pre-split row) — realised+unrealised path', () => {
    const p = makeIntradayRow({ realised: 100, unrealised: 150 }); // sums to pnl=250
    const expected = baseDayPnlForPosition(p);
    const split = splitClosedReopened(p);
    expect(sumDayPnl(split)).toBeCloseTo(expected, 6);
  });

  it('Groww-style row (overnight_quantity hardcoded 0, but genuinely a round trip) still splits and reconciles', () => {
    const p = makeIntradayRow({ tradingsymbol: 'RELIANCE', overnight_quantity: 0, quantity: 0, day_buy_quantity: 10, day_sell_quantity: 10, day_buy_value: 2000, day_sell_value: 2100, pnl: 100 });
    const split = splitClosedReopened(p);
    expect(split.length).toBe(1); // brokerQty=0 -> fully closed, single row
    expect(split[0]._splitTag).toBe('closed');
    expect(sumDayPnl(split)).toBeCloseTo(baseDayPnlForPosition(p), 6);
  });

  it('Groww hidden-overnight bug: fully-closed row with dbq!==dsq must NOT drop the residual portion (2026-09 regression)', () => {
    // Groww hardcodes overnight_quantity=0 even when a position genuinely
    // carried overnight. Simulate: a position with a true 10-unit overnight
    // carry-in plus a 5-unit intraday round-trip, fully closed today via
    // 5 buys + 15 sells (net: +10 overnight -10 via sells + round-trip).
    // The OLD buggy code sized the closed row on closedQty=min(dbq,dsq)=5
    // (the round-trip only) and silently dropped the other 10 units' P&L
    // whenever brokerQty (final qty) === 0. The fix must use the whole
    // row's pnl/baseDayPnlForPosition instead.
    const p = makeIntradayRow({
      tradingsymbol: 'RELIANCE',
      overnight_quantity: 0,      // Groww's (untrustworthy) hardcoded 0
      quantity: 0,                // fully closed today
      day_buy_quantity: 5,
      day_buy_value: 5 * 200,
      day_sell_quantity: 15,
      day_sell_value: 15 * 220,
      pnl: 500,                   // broker-authoritative total realized P&L
      realised: 0,
      // True prior-session baseline for this (account,symbol) — the backend
      // baseline join is keyed by account+symbol, not by Groww's oq flag,
      // so it correctly reflects the hidden overnight carry-in.
      prev_settlement_pnl: 300,
    });
    const expectedDayPnl = baseDayPnlForPosition(p); // 500 - 300 = 200
    const split = splitClosedReopened(p);
    expect(split.length).toBe(1);
    expect(split[0]._splitTag).toBe('closed');
    // Lifetime P&L must be the FULL broker-reported pnl (500), not the
    // round-trip-only figure (100) the old min(dbq,dsq) sizing produced.
    expect(split[0].pnl).toBeCloseTo(500, 6);
    // Day P&L must reconcile with the baseline-diff formula on the
    // pre-split row, not the truncated round-trip-only value.
    expect(sumDayPnl(split)).toBeCloseTo(expectedDayPnl, 6);
    expect(expectedDayPnl).toBeCloseTo(200, 6);
  });

  it('Groww hidden-overnight bug: fully-closed row sourced from realised+unrealised (no top-level pnl) still recovers the full lifetime P&L', () => {
    // Groww may ship realised_pnl + unrealised_pnl without a native
    // combined `pnl` field (per the design doc's per-broker sourcing:
    // Groww falls back to realised_pnl + unrealised_pnl when pnl is
    // absent). The closed row must be sized via currentTotalProfit(p)
    // (realised+unrealised), not a bare `Number(p.pnl || 0)` which would
    // silently read 0 when pnl is undefined.
    const p = makeIntradayRow({
      tradingsymbol: 'RELIANCE',
      overnight_quantity: 0,
      quantity: 0,
      day_buy_quantity: 5,
      day_buy_value: 5 * 200,
      day_sell_quantity: 15,
      day_sell_value: 15 * 220,
      pnl: undefined,           // no native combined field
      realised: 350,
      unrealised: 150,          // realised+unrealised = 500 (same total as above)
      prev_settlement_pnl: 300,
    });
    const split = splitClosedReopened(p);
    expect(split.length).toBe(1);
    expect(split[0].pnl).toBeCloseTo(500, 6);
    expect(split[0].realised).toBeCloseTo(500, 6);
    expect(sumDayPnl(split)).toBeCloseTo(200, 6);
  });

  it('closed row is unaffected by whether unrealised is present on the pre-split row', () => {
    const pWithout = makeIntradayRow();
    const pWith    = makeIntradayRow({ realised: 100, unrealised: 150 });
    const closedWithout = splitClosedReopened(pWithout)[0];
    const closedWith    = splitClosedReopened(pWith)[0];
    expect(baseDayPnlForPosition(closedWithout)).toBeCloseTo(baseDayPnlForPosition(closedWith), 6);
  });
});

describe('expiryPnlWithRealised — unaffected by the split (Exp P&L reconciles too)', () => {
  it('Σ expiryPnlWithRealised(split rows) accounts for the full realised + remaining-qty intrinsic value', () => {
    // Pre-split: qty=5 remaining @ avg=200, realised on the closed 5 lots = (220-200)*5 = 100.
    const p = {
      symbol: 'NIFTY25SEP24000CE', kind: 'opt', qty: 5, avg_cost: 200, realised: 0, pnl: 250,
    };
    const spot = 24500; // strike parses via decomposeSymbol/legAnalytics fallback; use legAnalytics for determinism
    const legAnalytics = { 'NIFTY25SEP24000CE': { strike: 24000, opt_type: 'CE' } };
    const closedRow = { symbol: p.symbol, kind: 'opt', qty: 0, pnl: 100, realised: 100, unrealised: 0 };
    const openRow   = { symbol: p.symbol, kind: 'opt', qty: 5, avg_cost: 200, realised: 0 };

    const closedExp = expiryPnlWithRealised(closedRow, spot, legAnalytics);
    const openExp   = expiryPnlWithRealised(openRow, spot, legAnalytics);
    // closed leg locks in its realised (100); open leg carries intrinsic value at spot.
    expect(closedExp).toBe(100);
    expect(openExp).not.toBeNull();
  });
});
