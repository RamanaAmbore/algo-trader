/**
 * positions_holdings_ssot.test.js
 *
 * Verifies that mergePositionRows produces correct day_pnl via livePositionDayPnl,
 * and that pure overnight positions agree with mergeHoldingRows (which uses the
 * same (ltp−close)×qty formula — valid for holdings because they never have
 * intraday adds).
 *
 * Five quality dimensions:
 *   1. SSOT   — pure overnight positions: both functions derive (ltp−close)×qty
 *   2. Perf   — pure unit, no DOM / network, sub-millisecond
 *   3. Stale  — market-closed scenario still returns correct value (no zero)
 *   4. Reuse  — exercises exported mergePositionRows / mergeHoldingRows helpers
 *   5. UX     — fallback to baseDayPnlForPosition (no live tick) tested
 */

import { describe, it, expect } from 'vitest';
import { mergePositionRows, mergeHoldingRows } from '../../data/pulseUnified.js';
import { baseDayPnlForPosition, livePositionDayPnl } from '$lib/data/nav.js';

// ── Helpers ──────────────────────────────────────────────────────────────────

function makePositionCtx(snapMap = {}) {
  return {
    snapOf: (sym) => snapMap[sym] ?? null,
    getInst: null,
    isMarketOpen: () => false,  // deliberately false — livePositionDayPnl ignores this
    baseDayPnlForPosition,
    livePositionDayPnl,
  };
}

function makeHoldingCtx(snapMap = {}) {
  return {
    snapOf: (sym) => snapMap[sym] ?? null,
    getInst: null,
    isMarketOpen: () => false,
  };
}

// Self-consistent position row: oq=overnight, dcv=oq*(last_price−close).
// avg is set so pnl=(ltp−avg)*qty is plausible but day_pnl only depends on dcv/close.
function makePositionRow(overrides = {}) {
  return {
    tradingsymbol:      'INFY25AUGFUT',
    exchange:           'NFO',
    quantity:           10,
    average_price:      990,   // below close so pnl is realistic
    close_price:        1000,
    last_price:         1005,
    pnl:                150,   // (1005−990)*10
    day_change_val:     50,    // oq*(last_price−close) = 10*(1005−1000)
    overnight_quantity: 10,
    realised:           0,
    ...overrides,
  };
}

function makeHoldingRow(overrides = {}) {
  return {
    tradingsymbol:      'INFY',
    exchange:           'NSE',
    quantity:           10,
    opening_quantity:   10,
    average_price:      990,
    close_price:        1000,
    last_price:         1005,
    pnl:                150,
    day_change_val:     50,
    ...overrides,
  };
}

// ── Test 1: live tick fires regardless of isMarketOpen in ctx ─────────────────

describe('mergePositionRows — live tick fires regardless of ctx.isMarketOpen', () => {
  it('ltp=1005, close=1000, qty=10 → day_pnl=50 (isMarketOpen=false in ctx)', () => {
    // livePositionDayPnl is called with marketOpen:true always.
    // ctx.isMarketOpen is not forwarded → no gate → live tick is used.
    // brokerDcv = baseDayPnlForPosition(r): oq=10, dcv=50 → returns 50
    // realisedToday = 50 − (1005−1000)×10 = 0
    // result = 0 + (1005−1000)×10 = 50
    const snapMap = { INFY25AUGFUT: { ltp: 1005, ltp_ts: 1 } };
    const byKey = {};
    mergePositionRows(byKey, [makePositionRow()], true, {}, makePositionCtx(snapMap));
    const row = Object.values(byKey)[0];
    expect(row.day_pnl).toBeCloseTo(50, 4);
  });

  it('snap ltp wins over liveQ ltp — symbolStore is primary source', () => {
    const snapMap = { INFY25AUGFUT: { ltp: 1005, ltp_ts: 1 } };
    const liveQ   = { 'NFO:INFY25AUGFUT': { ltp: 999 } };
    const byKey   = {};
    mergePositionRows(byKey, [makePositionRow()], true, liveQ, makePositionCtx(snapMap));
    const row = Object.values(byKey)[0];
    // snap (1005) wins: (1005−1000)×10 = 50, not (999−1000)×10 = −10
    expect(row.day_pnl).toBeCloseTo(50, 4);
  });

  it('falls back to liveQ ltp when snap has no ltp', () => {
    const liveQ = { 'NFO:INFY25AUGFUT': { ltp: 1005 } };
    const byKey = {};
    mergePositionRows(byKey, [makePositionRow()], true, liveQ, makePositionCtx());
    const row = Object.values(byKey)[0];
    expect(row.day_pnl).toBeCloseTo(50, 4);
  });
});

// ── Test 2: fallback to baseDayPnlForPosition when no live tick ──────────────

describe('mergePositionRows — fallback to baseDayPnlForPosition', () => {
  it('uses baseDayPnlForPosition (dcv path) when no snap and no liveQ ltp', () => {
    // No snap → liveLtp=null → livePositionDayPnl falls through to baseDayPnlForPosition.
    // row: oq=10, dcv=50, oq!==0 && dcv!==0 → returns dcv=50.
    const byKey = {};
    mergePositionRows(byKey, [makePositionRow()], true, {}, makePositionCtx({}));
    const row = Object.values(byKey)[0];
    expect(row.day_pnl).toBeCloseTo(50, 4);
  });

  it('ltp=0 guard: falls back to baseDayPnlForPosition (dcv=50)', () => {
    // ltp=0 fails the > 0 guard in livePositionDayPnl → fallback to brokerDcv=50.
    const snapMap = { INFY25AUGFUT: { ltp: 0, ltp_ts: 1 } };
    const byKey   = {};
    mergePositionRows(byKey, [makePositionRow()], true, {}, makePositionCtx(snapMap));
    const row = Object.values(byKey)[0];
    expect(row.day_pnl).toBeCloseTo(50, 4);
  });

  it('close_price=0 guard: falls back to baseDayPnlForPosition (new position — pnl path)', () => {
    // close=0 → livePositionDayPnl first branch fails (closePx must be > 0).
    // Second branch: close===0, avg>0, qty!==0 → returns (liveLtp−avg)*qty.
    // avg=990, liveLtp=1005, qty=10 → (1005−990)*10 = 150.
    const snapMap = { INFY25AUGFUT: { ltp: 1005, ltp_ts: 1 } };
    const byKey   = {};
    mergePositionRows(
      byKey,
      [makePositionRow({ close_price: 0, overnight_quantity: 0, day_change_val: 0, pnl: 150 })],
      true,
      {},
      makePositionCtx(snapMap),
    );
    const row = Object.values(byKey)[0];
    // livePositionDayPnl new-position branch: (1005−990)×10 = 150
    expect(row.day_pnl).toBeCloseTo(150, 4);
  });
});

// ── Test 3: symmetry — pure overnight positions agree with holdings ────────────

describe('Formula symmetry — pure overnight position = holdings for same inputs', () => {
  it('produces equal day_pnl for same ltp/close/qty (pure overnight position)', () => {
    // For a pure overnight position (no intraday adds), livePositionDayPnl reduces to
    // (ltp−close)×qty — same as holdings. Symmetry holds.
    const ltp   = 1020;
    const close = 1000;
    const qty   = 10;
    const dcv   = qty * (ltp - close);  // 200 — self-consistent

    const posSnap  = { INFY25AUGFUT: { ltp, ltp_ts: 1 } };
    const holdSnap = { INFY: { ltp } };

    const posByKey  = {};
    const holdByKey = {};

    mergePositionRows(
      posByKey,
      [makePositionRow({ close_price: close, quantity: qty, last_price: ltp,
                         overnight_quantity: qty, day_change_val: dcv })],
      true,
      {},
      makePositionCtx(posSnap),
    );

    mergeHoldingRows(
      holdByKey,
      [makeHoldingRow({ close_price: close, quantity: qty, opening_quantity: qty, last_price: ltp })],
      true,
      {},
      makeHoldingCtx(holdSnap),
    );

    const posRow  = Object.values(posByKey)[0];
    const holdRow = Object.values(holdByKey)[0];

    expect(posRow.day_pnl).toBeCloseTo(200, 4);
    expect(holdRow.day_pnl).toBeCloseTo(200, 4);
    expect(posRow.day_pnl).toBeCloseTo(holdRow.day_pnl, 4);
  });

  it('symmetry holds for short positions (qty < 0)', () => {
    const ltp   = 980;
    const close = 1000;
    const qty   = -5;
    const dcv   = qty * (ltp - close);  // 100

    const posSnap = { INFY25AUGFUT: { ltp, ltp_ts: 1 } };
    const posByKey = {};

    mergePositionRows(
      posByKey,
      [makePositionRow({ close_price: close, quantity: qty, last_price: ltp,
                         overnight_quantity: qty, day_change_val: dcv })],
      true,
      {},
      makePositionCtx(posSnap),
    );

    const posRow = Object.values(posByKey)[0];
    expect(posRow.day_pnl).toBeCloseTo(100, 4);
  });

  it('symmetry holds for negative day move (ltp < close)', () => {
    const ltp   = 990;
    const close = 1000;
    const qty   = 10;
    const dcv   = qty * (ltp - close);  // -100

    const posSnap  = { INFY25AUGFUT: { ltp, ltp_ts: 1 } };
    const holdSnap = { INFY: { ltp } };
    const posByKey  = {};
    const holdByKey = {};

    mergePositionRows(
      posByKey,
      [makePositionRow({ close_price: close, quantity: qty, last_price: ltp,
                         overnight_quantity: qty, day_change_val: dcv })],
      true,
      {},
      makePositionCtx(posSnap),
    );

    mergeHoldingRows(
      holdByKey,
      [makeHoldingRow({ close_price: close, quantity: qty, opening_quantity: qty, last_price: ltp })],
      true,
      {},
      makeHoldingCtx(holdSnap),
    );

    const posRow  = Object.values(posByKey)[0];
    const holdRow = Object.values(holdByKey)[0];

    expect(posRow.day_pnl).toBeCloseTo(-100, 4);
    expect(holdRow.day_pnl).toBeCloseTo(-100, 4);
    expect(posRow.day_pnl).toBeCloseTo(holdRow.day_pnl, 4);
  });
});
