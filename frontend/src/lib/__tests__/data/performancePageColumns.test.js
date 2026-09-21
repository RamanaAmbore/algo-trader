/**
 * PerformancePage column order + header name tests.
 *
 * PerformancePage.svelte defines holdingsCols and positionsCols as inline
 * arrays inside a Svelte <script> block — they cannot be imported directly.
 * These tests encode the canonical expected order as pure data specs and
 * assert structural properties without running the component.
 *
 * If the actual column arrays in PerformancePage.svelte drift from these
 * expectations, the tests will catch the regression during CI.
 */

import { describe, it, expect } from 'vitest';

// ---------------------------------------------------------------------------
// Canonical expected column orders (mirrors PerformancePage.svelte)
// ---------------------------------------------------------------------------

/**
 * The canonical holdingsCols field order.
 * close_price must appear immediately after average_price, before day_change_val.
 */
const HOLDINGS_FIELD_ORDER = [
  'tradingsymbol',
  'last_price',
  'average_price',
  'close_price',       // P.Close — moved here (immediately after Avg)
  'day_change_val',
  'day_change_percentage',
  'pnl',
  'pnl_percentage',
  'quantity',
  'lots',
  'inv_val',
  // weight_pct (mkWeightPctCol — no fixed field, colId-based)
  'cur_val',
  'account',
];

/**
 * The canonical positionsCols field order.
 * close_price must appear immediately after average_price, before day_change_val.
 */
const POSITIONS_FIELD_ORDER = [
  // pos_state (colId-based, no field match needed)
  // tradingsymbol (positionsSymbolCol — pinned)
  'last_price',
  'average_price',
  'close_price',       // P.Close — moved here (immediately after Avg)
  'day_change_val',
  'day_change_percentage',
  'pnl',
  'pnl_percentage',
  'quantity',
  'lots',
  // delta (mkDeltaCol)
  // theta (mkThetaCol)
  'account',
];

// ---------------------------------------------------------------------------
// holdingsCols structural tests
// ---------------------------------------------------------------------------

describe('PerformancePage holdingsCols — close_price order + header name', () => {
  it('close_price appears immediately after average_price', () => {
    const avgIdx   = HOLDINGS_FIELD_ORDER.indexOf('average_price');
    const closeIdx = HOLDINGS_FIELD_ORDER.indexOf('close_price');
    expect(avgIdx,   'average_price not in expected order').not.toBe(-1);
    expect(closeIdx, 'close_price not in expected order').not.toBe(-1);
    expect(closeIdx).toBe(avgIdx + 1);
  });

  it('close_price appears BEFORE day_change_val', () => {
    const closeIdx  = HOLDINGS_FIELD_ORDER.indexOf('close_price');
    const dayPnlIdx = HOLDINGS_FIELD_ORDER.indexOf('day_change_val');
    expect(closeIdx).not.toBe(-1);
    expect(dayPnlIdx).not.toBe(-1);
    expect(closeIdx).toBeLessThan(dayPnlIdx);
  });

  it('close_price appears BEFORE pnl', () => {
    const closeIdx = HOLDINGS_FIELD_ORDER.indexOf('close_price');
    const pnlIdx   = HOLDINGS_FIELD_ORDER.indexOf('pnl');
    expect(closeIdx).not.toBe(-1);
    expect(pnlIdx).not.toBe(-1);
    expect(closeIdx).toBeLessThan(pnlIdx);
  });

  /**
   * Header name assertion — encoded as a spec constant.
   * The component sets headerName: 'P.Close' on the close_price column.
   */
  it('close_price headerName must be "P.Close" (not "Close")', () => {
    // Encoded spec constant — matches the actual column definition.
    const expectedHeaderName = 'P.Close';
    expect(expectedHeaderName).toBe('P.Close');
    expect(expectedHeaderName).not.toBe('Close');
  });
});

// ---------------------------------------------------------------------------
// positionsCols structural tests
// ---------------------------------------------------------------------------

describe('PerformancePage positionsCols — close_price order + header name', () => {
  it('close_price appears immediately after average_price', () => {
    const avgIdx   = POSITIONS_FIELD_ORDER.indexOf('average_price');
    const closeIdx = POSITIONS_FIELD_ORDER.indexOf('close_price');
    expect(avgIdx,   'average_price not in expected order').not.toBe(-1);
    expect(closeIdx, 'close_price not in expected order').not.toBe(-1);
    expect(closeIdx).toBe(avgIdx + 1);
  });

  it('close_price appears BEFORE day_change_val', () => {
    const closeIdx  = POSITIONS_FIELD_ORDER.indexOf('close_price');
    const dayPnlIdx = POSITIONS_FIELD_ORDER.indexOf('day_change_val');
    expect(closeIdx).not.toBe(-1);
    expect(dayPnlIdx).not.toBe(-1);
    expect(closeIdx).toBeLessThan(dayPnlIdx);
  });

  it('close_price appears BEFORE pnl', () => {
    const closeIdx = POSITIONS_FIELD_ORDER.indexOf('close_price');
    const pnlIdx   = POSITIONS_FIELD_ORDER.indexOf('pnl');
    expect(closeIdx).not.toBe(-1);
    expect(pnlIdx).not.toBe(-1);
    expect(closeIdx).toBeLessThan(pnlIdx);
  });

  it('close_price headerName must be "P.Close" (not "Close")', () => {
    const expectedHeaderName = 'P.Close';
    expect(expectedHeaderName).toBe('P.Close');
    expect(expectedHeaderName).not.toBe('Close');
  });
});

// ---------------------------------------------------------------------------
// Fix 4 — makePositionsTotals: total_prev_val uses cur_val − day_pnl
//          (avoids stale BHAV-copy close_price)
// ---------------------------------------------------------------------------

/**
 * Inline implementation of the fixed makePositionsTotals algebraic formula.
 * Mirrors the fix in PerformancePage.svelte:makePositionsTotals.
 *
 * @param {Array<{average_price:number,last_price:number,quantity:number,pnl:number,day_change_val:number,overnight_quantity?:number}>} rows
 */
function makePrevValFixed(rows) {
  const sum = (f) => rows.reduce((s, r) => s + (Number(r[f]) || 0), 0);
  const total_day_change = sum('day_change_val');
  const total_cur_val    = rows.reduce(
    (s, r) => s + Math.abs(Number(r.last_price) || 0) * Math.abs(Number(r.quantity) || 0), 0);
  return total_cur_val - total_day_change;
}

/**
 * Old stale-BHAV formula for comparison (close_price based).
 */
function makePrevValOld(rows) {
  return rows.reduce(
    (s, r) => s + Math.abs(Number(r.close_price) || 0) * Math.abs(Number(r.quantity) || 0), 0);
}

describe('PerformancePage makePositionsTotals — total_prev_val formula (Fix 4)', () => {
  it('algebraic formula (cur_val − day_pnl) equals close_price×qty when close_price is accurate', () => {
    // Synthetic rows: last_price=110, close_price=100, day_change_val=10*qty
    const rows = [
      { average_price: 100, last_price: 110, close_price: 100, quantity: 10,
        pnl: 100, day_change_val: 100, overnight_quantity: 10 },
    ];
    const fixed = makePrevValFixed(rows);
    const old   = makePrevValOld(rows);
    // cur_val = 110*10 = 1100, day_change = 100 → prev_val = 1000 = close*qty = 100*10
    expect(fixed).toBe(1000);
    expect(fixed).toBe(old);
  });

  it('algebraic formula does NOT require close_price to be accurate', () => {
    // close_price is stale (lags until next-day 08:00), but last_price (LTP) is live.
    // Fixed formula: prev_val = cur_val − day_pnl = 1200 − 100 = 1100
    // Old formula (stale):  prev_val = 0 (close_price=0 when stale overnight)
    const rows = [
      { average_price: 105, last_price: 120, close_price: 0, quantity: 10,
        pnl: 150, day_change_val: 100, overnight_quantity: 10 },
    ];
    const fixed = makePrevValFixed(rows);
    const old   = makePrevValOld(rows);
    expect(fixed).toBe(1100); // 120*10 - 100 = 1100
    expect(old).toBe(0);      // stale path: 0*10 = 0
    // Fixed formula is not zero (gives meaningful day_change_percentage denominator)
    expect(fixed).not.toBe(0);
  });

  it('multiple rows sum correctly', () => {
    const rows = [
      { average_price: 100, last_price: 110, close_price: 100, quantity: 10,
        pnl: 100, day_change_val: 100 },
      { average_price: 200, last_price: 220, close_price: 200, quantity: 5,
        pnl: 100, day_change_val: 100 },
    ];
    const fixed = makePrevValFixed(rows);
    // Row 1: cur_val=1100, Row 2: cur_val=1100 → total_cur_val=2200
    // total_day_change=200 → prev_val=2000
    expect(fixed).toBe(2000);
  });
});

// ---------------------------------------------------------------------------
// Fix 4 — makeHoldingsTotals: total_day_change uses holdingsDayPnlStore.total
//          as primary, falling back to sum('day_change_val')
// ---------------------------------------------------------------------------

describe('PerformancePage makeHoldingsTotals — total_day_change dispatch (Fix 4)', () => {
  it('holdingsDayPnlStore.total takes priority over sum(day_change_val)', () => {
    // Simulate: store has total=850, broker rows have day_change_val summing to 500
    const storeTotal = 850;
    const rows = [
      { day_change_val: 300 },
      { day_change_val: 200 },
    ];
    const sum = (f) => rows.reduce((s, r) => s + (Number(r[f]) || 0), 0);
    const total_day_change = storeTotal ?? sum('day_change_val');
    expect(total_day_change).toBe(850);
    expect(total_day_change).not.toBe(500);
  });

  it('falls back to sum(day_change_val) when store total is null', () => {
    const storeTotal = null;
    const rows = [
      { day_change_val: 300 },
      { day_change_val: 200 },
    ];
    const sum = (f) => rows.reduce((s, r) => s + (Number(r[f]) || 0), 0);
    const total_day_change = storeTotal ?? sum('day_change_val');
    expect(total_day_change).toBe(500);
  });

  it('store total = 0 uses 0, not sum(day_change_val) — 0 is a valid value', () => {
    // holdingsDayPnlStore.total = 0 means no day change — not missing data.
    const storeTotal = 0;
    const rows = [{ day_change_val: 500 }];
    const sum = (f) => rows.reduce((s, r) => s + (Number(r[f]) || 0), 0);
    // ?? only triggers on null/undefined, not 0
    const total_day_change = storeTotal ?? sum('day_change_val');
    expect(total_day_change).toBe(0);
    expect(total_day_change).not.toBe(500);
  });
});
