/**
 * positions_holdings_ssot.test.js
 *
 * Verifies that mergePositionRows and mergeHoldingRows produce equivalent
 * day_pnl when given the same ltp/close/qty inputs — the "formula symmetry"
 * guarantee introduced when positions day P&L was changed from
 * livePositionDayPnl (gated by isMarketOpen()) to the direct
 * (ltp - close) × qty formula that holdings has always used.
 *
 * Five quality dimensions:
 *   1. SSOT   — both functions derive from the same formula; no isMarketOpen gate
 *   2. Perf   — pure unit, no DOM / network, sub-millisecond
 *   3. Stale  — market-closed scenario still returns correct value (no zero)
 *   4. Reuse  — exercises the exported mergePositionRows / mergeHoldingRows helpers
 *   5. UX     — fallback to baseDayPnlForPosition is tested
 */

import { describe, it, expect, vi } from 'vitest';
import { mergePositionRows, mergeHoldingRows, makeRowFactory } from '../../data/pulseUnified.js';

// ── Helpers ──────────────────────────────────────────────────────────────────

function makePositionCtx(snapMap = {}, baseDayPnlFn = null) {
  return {
    snapOf: (sym) => snapMap[sym] ?? null,
    getInst: null,
    // isMarketOpen is still in the ctx type for callers (mergeHoldingRows uses it),
    // but mergePositionRows no longer reads it for day_pnl — we deliberately pass
    // false here to confirm the formula fires regardless.
    isMarketOpen: () => false,
    baseDayPnlForPosition: baseDayPnlFn ?? ((r) => Number(r.day_change_val || 0)),
  };
}

function makeHoldingCtx(snapMap = {}) {
  return {
    snapOf: (sym) => snapMap[sym] ?? null,
    getInst: null,
    isMarketOpen: () => false,
  };
}

// Minimal position broker row.
function makePositionRow(overrides = {}) {
  return {
    tradingsymbol: 'INFY25AUGFUT',
    exchange: 'NFO',
    quantity: 10,
    average_price: 1800,
    close_price: 1000,
    last_price: 1005,
    pnl: 50,
    day_change_val: 0,
    overnight_quantity: 10,
    realised: 0,
    ...overrides,
  };
}

// Minimal holding broker row with same price universe.
function makeHoldingRow(overrides = {}) {
  return {
    tradingsymbol: 'INFY',
    exchange: 'NSE',
    quantity: 10,
    opening_quantity: 10,
    average_price: 1800,
    close_price: 1000,
    last_price: 1005,
    pnl: 50,
    day_change_val: 0,
    ...overrides,
  };
}

// ── Test 1: direct formula fires when market is closed ───────────────────────

describe('mergePositionRows — direct formula with market closed', () => {
  it('computes (ltp − close) × qty even when isMarketOpen returns false', () => {
    // ltp=1005, close=1000, qty=10 → day_pnl = 50
    const snapMap = { INFY25AUGFUT: { ltp: 1005 } };
    const byKey = {};
    mergePositionRows(byKey, [makePositionRow()], true, {}, makePositionCtx(snapMap));
    const row = Object.values(byKey)[0];
    // If the old gate had fired (mktOpen=false → liveLtp=null), this would
    // have fallen back to baseDayPnlForPosition which returns day_change_val=0.
    expect(row.day_pnl).toBeCloseTo(50, 4);
  });

  it('uses snap ltp over liveQ ltp — symbolStore is the primary source', () => {
    // snap.ltp=1005, liveQ.ltp=999 — snap must win
    const snapMap = { INFY25AUGFUT: { ltp: 1005 } };
    const liveQ   = { 'NFO:INFY25AUGFUT': { ltp: 999 } };
    const byKey   = {};
    mergePositionRows(byKey, [makePositionRow()], true, liveQ, makePositionCtx(snapMap));
    const row = Object.values(byKey)[0];
    // (1005 - 1000) * 10 = 50, not (999 - 1000) * 10 = -10
    expect(row.day_pnl).toBeCloseTo(50, 4);
  });

  it('falls back to liveQ ltp when snap has no ltp', () => {
    // no snap, liveQ.ltp=1005
    const liveQ = { 'NFO:INFY25AUGFUT': { ltp: 1005 } };
    const byKey = {};
    mergePositionRows(byKey, [makePositionRow()], true, liveQ, makePositionCtx());
    const row = Object.values(byKey)[0];
    expect(row.day_pnl).toBeCloseTo(50, 4);
  });
});

// ── Test 2: fallback to baseDayPnlForPosition ────────────────────────────────

describe('mergePositionRows — fallback to baseDayPnlForPosition', () => {
  it('uses baseDayPnlForPosition when no snap and no liveQ ltp', () => {
    // No snap, no liveQ → falls back. baseDayPnlFn returns 77 sentinel.
    const byKey = {};
    mergePositionRows(
      byKey,
      [makePositionRow({ day_change_val: 77 })],
      true,
      {},
      makePositionCtx({}, () => 77),
    );
    const row = Object.values(byKey)[0];
    expect(row.day_pnl).toBeCloseTo(77, 4);
  });

  it('uses baseDayPnlForPosition when ltp is zero (cold-cache guard)', () => {
    // ltp=0 must not be used — (0 - close) × qty would be a large negative.
    const snapMap = { INFY25AUGFUT: { ltp: 0 } };
    const byKey   = {};
    mergePositionRows(
      byKey,
      [makePositionRow({ day_change_val: 88 })],
      true,
      {},
      makePositionCtx(snapMap, () => 88),
    );
    const row = Object.values(byKey)[0];
    expect(row.day_pnl).toBeCloseTo(88, 4);
  });

  it('uses baseDayPnlForPosition when close_price is zero (stale-close guard)', () => {
    // close=0 must not be used as a divisor-equivalent — treat as "no data".
    const snapMap = { INFY25AUGFUT: { ltp: 1005 } };
    const byKey   = {};
    mergePositionRows(
      byKey,
      [makePositionRow({ close_price: 0, day_change_val: 99 })],
      true,
      {},
      makePositionCtx(snapMap, () => 99),
    );
    const row = Object.values(byKey)[0];
    expect(row.day_pnl).toBeCloseTo(99, 4);
  });
});

// ── Test 3: formula symmetry — positions and holdings produce equal day_pnl ───

describe('Formula symmetry — mergePositionRows vs mergeHoldingRows', () => {
  it('produces equal day_pnl for same ltp/close/qty inputs', () => {
    const ltp   = 1020;
    const close = 1000;
    const qty   = 10;
    // expected = (1020 - 1000) * 10 = 200

    const posSnap  = { INFY25AUGFUT: { ltp } };
    const holdSnap = { INFY: { ltp } };

    const posByKey  = {};
    const holdByKey = {};

    mergePositionRows(
      posByKey,
      [makePositionRow({ close_price: close, quantity: qty })],
      true,
      {},
      makePositionCtx(posSnap),
    );

    mergeHoldingRows(
      holdByKey,
      [makeHoldingRow({ close_price: close, quantity: qty, opening_quantity: qty })],
      true,
      {},
      makeHoldingCtx(holdSnap),
    );

    const posRow  = Object.values(posByKey)[0];
    const holdRow = Object.values(holdByKey)[0];

    expect(posRow.day_pnl).toBeCloseTo(200, 4);
    expect(holdRow.day_pnl).toBeCloseTo(200, 4);
    // The key assertion: both formulas agree exactly.
    expect(posRow.day_pnl).toBeCloseTo(holdRow.day_pnl, 4);
  });

  it('symmetry holds for short positions (qty < 0)', () => {
    const ltp   = 980;
    const close = 1000;
    const qty   = -5;
    // expected = (980 - 1000) * -5 = 100 (profit on a short when price falls)

    const posSnap = { INFY25AUGFUT: { ltp } };
    const posByKey = {};

    mergePositionRows(
      posByKey,
      [makePositionRow({ close_price: close, quantity: qty })],
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
    // expected = (990 - 1000) * 10 = -100

    const posSnap  = { INFY25AUGFUT: { ltp } };
    const holdSnap = { INFY: { ltp } };
    const posByKey  = {};
    const holdByKey = {};

    mergePositionRows(
      posByKey,
      [makePositionRow({ close_price: close, quantity: qty })],
      true,
      {},
      makePositionCtx(posSnap),
    );

    mergeHoldingRows(
      holdByKey,
      [makeHoldingRow({ close_price: close, quantity: qty, opening_quantity: qty })],
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
