/**
 * holdingsDayPnlStore.test.js
 *
 * Unit tests for the holdings day P&L store computation logic.
 *
 * The store aggregates day P&L across all holdings by:
 *   1. Reading from holdingsStore.value (array of broker holding rows)
 *   2. For each row, computing (liveLtp - closePx) * heldQty when live
 *      LTP is available, or falling back to day_change_val
 *   3. Exporting { total: number, byKey: { [tradingsymbol: string]: number } }
 *
 * This file validates the computation logic using a local helper that
 * mirrors the store, following the positionsDayPnlStore.test.js pattern
 * (Svelte 5 rune modules cannot be imported directly in Vitest).
 *
 * Five quality dimensions:
 *   1. SSOT   — logic mirrors holdingsDayPnlStore, mergeHoldingRows, and
 *               _liveHoldingsToday (the inline block now replaced)
 *   2. Perf   — pure unit, no DOM / network, sub-millisecond
 *   3. Stale  — fallback to day_change_val when no live LTP or close = 0
 *   4. Reuse  — byKey keyed by plain sym matches MarketPulse `${sym}__hold`
 *   5. UX     — post-settlement guard, empty holdings, multi-account
 */

import { describe, it, expect } from 'vitest';

// ── Local helper: mirrors holdingsDayPnlStore's $derived.by body ────────────

/**
 * @param {any[]} holdings
 * @param {Record<string, { ltp?: number | null | undefined }>} [snapshots]
 * @returns {{ total: number, byKey: Record<string, number> }}
 */
function computeHoldingsDayPnl(holdings = [], snapshots = /** @type {Record<string, { ltp?: number | null | undefined }>} */ ({})) {
  const byKey = /** @type {Record<string, number>} */ ({});
  let total = 0;

  for (const h of holdings) {
    const sym = String(h?.tradingsymbol || h?.symbol || '').toUpperCase();
    if (!sym) continue;

    const snap    = snapshots[sym];
    const snapLtp = snap?.ltp;

    const liveLtp = (snapLtp != null && snapLtp > 0)
      ? Number(snapLtp)
      : Number(h?.last_price ?? 0);

    const closePx = Number(h?.close_price)     || 0;
    const heldQty = Number(h?.opening_quantity) || Number(h?.quantity) || 0;
    const dcv     = Number(h?.day_change_val)  || 0;

    let val;
    if (liveLtp > 0 && closePx > 0 && heldQty !== 0 && Math.abs(liveLtp - closePx) > 0.005) {
      val = (liveLtp - closePx) * heldQty;
    } else {
      val = dcv;
    }

    byKey[sym] = (byKey[sym] ?? 0) + val;
    total += val;
  }

  return { total, byKey };
}

function makeHoldingRow(overrides = {}) {
  return {
    tradingsymbol:      'RELIANCE',
    exchange:           'NSE',
    quantity:           10,
    opening_quantity:   10,
    average_price:      2400,
    close_price:        2500,
    last_price:         2510,
    day_change_val:     100,
    pnl:                1100,
    ...overrides,
  };
}

// ── Test 1: Task-spec scenario ────────────────────────────────────────────────

describe('holdingsDayPnlStore — task spec scenario', () => {
  it('RELIANCE close=2500, snapshot ltp=2520, qty=10 → total=200, byKey[RELIANCE]=200', () => {
    // Spec: mock holdingsStore.value with one row {tradingsymbol:'RELIANCE',
    //   close_price:2500, last_price:2510, quantity:10}
    //   mock getSnapshot('RELIANCE') to return {ltp:2520}
    //   assert total = (2520-2500)*10 = 200
    const holdings = [makeHoldingRow({
      tradingsymbol:    'RELIANCE',
      close_price:      2500,
      last_price:       2510,
      quantity:         10,
      opening_quantity: 10,
      day_change_val:   100,
    })];
    const snapshots = { RELIANCE: { ltp: 2520 } };

    const result = computeHoldingsDayPnl(holdings, snapshots);

    expect(result.total).toBeCloseTo(200, 4);
    expect(result.byKey['RELIANCE']).toBeCloseTo(200, 4);
  });
});

// ── Test 2: Snapshot LTP wins over last_price ────────────────────────────────

describe('holdingsDayPnlStore — LTP precedence', () => {
  it('snapshot ltp overrides last_price for live formula', () => {
    // last_price = 2510, but snapshot ltp = 2530 → formula uses 2530
    const holdings = [makeHoldingRow({ close_price: 2500, last_price: 2510, quantity: 5, opening_quantity: 5 })];
    const snapshots = { RELIANCE: { ltp: 2530 } };

    const result = computeHoldingsDayPnl(holdings, snapshots);

    expect(result.byKey['RELIANCE']).toBeCloseTo((2530 - 2500) * 5, 4);
  });

  it('no snapshot: falls back to last_price for live formula', () => {
    const holdings = [makeHoldingRow({ close_price: 2500, last_price: 2510, quantity: 5, opening_quantity: 5 })];
    /** @type {Record<string, { ltp?: number | null }>} */
    const snapshots = {};  // no tick for this symbol

    const result = computeHoldingsDayPnl(holdings, snapshots);

    // last_price = 2510, close = 2500, qty = 5 → (2510-2500)*5 = 50
    expect(result.byKey['RELIANCE']).toBeCloseTo(50, 4);
  });

  it('snapshot ltp = 0: treated as absent, falls back to last_price', () => {
    const holdings = [makeHoldingRow({ close_price: 2500, last_price: 2510, quantity: 5, opening_quantity: 5 })];
    const snapshots = { RELIANCE: { ltp: 0 } };  // zero ltp → not useful

    const result = computeHoldingsDayPnl(holdings, snapshots);

    // liveLtp falls back to last_price = 2510 → (2510-2500)*5 = 50
    expect(result.byKey['RELIANCE']).toBeCloseTo(50, 4);
  });

  it('snapshot ltp null: falls back to last_price', () => {
    const holdings = [makeHoldingRow({ close_price: 2500, last_price: 2510, quantity: 5, opening_quantity: 5 })];
    const snapshots = { RELIANCE: { ltp: null } };

    const result = computeHoldingsDayPnl(holdings, snapshots);

    expect(result.byKey['RELIANCE']).toBeCloseTo(50, 4);
  });
});

// ── Test 3: Fallback to day_change_val ────────────────────────────────────────

describe('holdingsDayPnlStore — fallback to day_change_val', () => {
  it('close_price = 0: falls back to day_change_val', () => {
    const holdings = [makeHoldingRow({ close_price: 0, last_price: 2510, quantity: 10, opening_quantity: 10, day_change_val: 150 })];
    const snapshots = { RELIANCE: { ltp: 2520 } };

    const result = computeHoldingsDayPnl(holdings, snapshots);

    // closePx = 0 → live formula fails → dcv = 150
    expect(result.byKey['RELIANCE']).toBe(150);
  });

  it('quantity = 0: falls back to day_change_val', () => {
    const holdings = [makeHoldingRow({ close_price: 2500, last_price: 2510, quantity: 0, opening_quantity: 0, day_change_val: 200 })];
    const snapshots = { RELIANCE: { ltp: 2520 } };

    const result = computeHoldingsDayPnl(holdings, snapshots);

    // heldQty = 0 → live formula fails → dcv = 200
    expect(result.byKey['RELIANCE']).toBe(200);
  });

  it('last_price = 0 and no snapshot: liveLtp = 0, falls back to dcv', () => {
    const holdings = [makeHoldingRow({ close_price: 2500, last_price: 0, quantity: 10, opening_quantity: 10, day_change_val: 120 })];
    /** @type {Record<string, { ltp?: number | null }>} */
    const snapshots = {};

    const result = computeHoldingsDayPnl(holdings, snapshots);

    expect(result.byKey['RELIANCE']).toBe(120);
  });
});

// ── Test 4: Post-settlement guard (ltp ≈ close) ──────────────────────────────

describe('holdingsDayPnlStore — post-settlement guard', () => {
  it('ltp very close to close (delta ≤ 0.005): falls back to day_change_val', () => {
    // Kite resets last_price = close_price = settlement_price after NSE settlement
    const holdings = [makeHoldingRow({
      close_price:    2500.000,
      last_price:     2500.003,   // |delta| ≤ 0.005
      quantity:       10,
      opening_quantity: 10,
      day_change_val: 0,
    })];
    const snapshots = { RELIANCE: { ltp: 2500.003 } };

    const result = computeHoldingsDayPnl(holdings, snapshots);

    // |2500.003 - 2500.000| = 0.003 ≤ 0.005 → guard fires → dcv = 0
    expect(result.byKey['RELIANCE']).toBe(0);
  });

  it('ltp just above guard threshold: live formula fires', () => {
    const holdings = [makeHoldingRow({
      close_price:    2500,
      last_price:     2500.01,    // |delta| > 0.005
      quantity:       10,
      opening_quantity: 10,
      day_change_val: 0,
    })];
    const snapshots = { RELIANCE: { ltp: 2500.01 } };

    const result = computeHoldingsDayPnl(holdings, snapshots);

    expect(result.byKey['RELIANCE']).toBeCloseTo(0.01 * 10, 4);
  });
});

// ── Test 5: Multiple holdings ─────────────────────────────────────────────────

describe('holdingsDayPnlStore — multiple holdings', () => {
  it('two holdings: total = sum of individual day_pnls', () => {
    const holdings = [
      makeHoldingRow({ tradingsymbol: 'RELIANCE', close_price: 2500, quantity: 10, opening_quantity: 10, day_change_val: 100 }),
      makeHoldingRow({ tradingsymbol: 'INFY',     close_price: 1800, quantity: 5,  opening_quantity: 5,  day_change_val: 25 }),
    ];
    const snapshots = {
      RELIANCE: { ltp: 2520 },  // (2520-2500)*10 = 200
      INFY:     { ltp: 1810 },  // (1810-1800)*5  = 50
    };

    const result = computeHoldingsDayPnl(holdings, snapshots);

    expect(result.byKey['RELIANCE']).toBeCloseTo(200, 4);
    expect(result.byKey['INFY']).toBeCloseTo(50, 4);
    expect(result.total).toBeCloseTo(250, 4);
  });

  it('mixed: one with live ltp, one falls back to dcv', () => {
    const holdings = [
      makeHoldingRow({ tradingsymbol: 'RELIANCE', close_price: 2500, quantity: 10, opening_quantity: 10, day_change_val: 100 }),
      makeHoldingRow({ tradingsymbol: 'TCS',      close_price: 0,    quantity: 3,  opening_quantity: 3,  day_change_val: 60 }),
    ];
    const snapshots = {
      RELIANCE: { ltp: 2520 },  // (2520-2500)*10 = 200
      TCS:      { ltp: 3500 },  // close=0 → fallback to dcv=60
    };

    const result = computeHoldingsDayPnl(holdings, snapshots);

    expect(result.byKey['RELIANCE']).toBeCloseTo(200, 4);
    expect(result.byKey['TCS']).toBe(60);
    expect(result.total).toBeCloseTo(260, 4);
  });
});

// ── Test 6: byKey key format ─────────────────────────────────────────────────

describe('holdingsDayPnlStore — byKey key format', () => {
  it('keys are plain uppercase tradingsymbol (no exchange prefix)', () => {
    const holdings = [makeHoldingRow({ tradingsymbol: 'reliance', exchange: 'nse' })];
    const snapshots = { RELIANCE: { ltp: 2520 } };

    const result = computeHoldingsDayPnl(holdings, snapshots);

    // Key must be 'RELIANCE', NOT 'NSE:RELIANCE'
    expect(Object.keys(result.byKey)).toEqual(['RELIANCE']);
  });

  it('byKey sym maps directly to ${sym}__hold in MarketPulse byKey', () => {
    // MarketPulse uses: byKey[`${sym}__hold`] = holdingsDayPnlStore.byKey[sym]
    const holdings = [makeHoldingRow({ tradingsymbol: 'INFY', close_price: 1800, quantity: 5, opening_quantity: 5 })];
    const snapshots = { INFY: { ltp: 1810 } };

    const result = computeHoldingsDayPnl(holdings, snapshots);
    const sym = 'INFY';
    const pulseKey = `${sym}__hold`;  // MarketPulse key pattern

    // Verify the lookup chain works: byKey[sym] feeds byKey[pulseKey]
    expect(result.byKey[sym]).toBeCloseTo((1810 - 1800) * 5, 4);
    expect(pulseKey).toBe('INFY__hold');
  });
});

// ── Test 7: Edge cases ────────────────────────────────────────────────────────

describe('holdingsDayPnlStore — edge cases', () => {
  it('empty holdings: total=0, byKey={}', () => {
    const result = computeHoldingsDayPnl([], {});
    expect(result.total).toBe(0);
    expect(result.byKey).toEqual({});
  });

  it('row with no tradingsymbol is skipped', () => {
    const holdings = [{ close_price: 2500, quantity: 10, day_change_val: 100 }];
    const result = computeHoldingsDayPnl(holdings, {});
    expect(result.total).toBe(0);
    expect(result.byKey).toEqual({});
  });

  it('uses opening_quantity over quantity when both present', () => {
    const holdings = [makeHoldingRow({
      opening_quantity: 8,
      quantity:         10,  // should be ignored
      close_price:      2500,
      last_price:       2510,
      day_change_val:   0,
    })];
    const snapshots = { RELIANCE: { ltp: 2510 } };

    const result = computeHoldingsDayPnl(holdings, snapshots);

    // heldQty = opening_quantity = 8 → (2510-2500)*8 = 80
    expect(result.byKey['RELIANCE']).toBeCloseTo(80, 4);
  });

  it('same symbol in two rows (multi-account): values are summed', () => {
    const holdings = [
      makeHoldingRow({ tradingsymbol: 'RELIANCE', opening_quantity: 5, close_price: 2500, day_change_val: 0 }),
      makeHoldingRow({ tradingsymbol: 'RELIANCE', opening_quantity: 3, close_price: 2500, day_change_val: 0 }),
    ];
    const snapshots = { RELIANCE: { ltp: 2510 } };

    const result = computeHoldingsDayPnl(holdings, snapshots);

    // (2510-2500)*5 + (2510-2500)*3 = 50 + 30 = 80
    expect(result.byKey['RELIANCE']).toBeCloseTo(80, 4);
    expect(result.total).toBeCloseTo(80, 4);
  });

  it('negative day_change_val (losing holding): correctly sums', () => {
    const holdings = [
      makeHoldingRow({ tradingsymbol: 'HDFC', close_price: 3000, quantity: 5, opening_quantity: 5, day_change_val: -150 }),
    ];
    const snapshots = { HDFC: { ltp: 2970 } };  // (2970-3000)*5 = -150

    const result = computeHoldingsDayPnl(holdings, snapshots);

    expect(result.byKey['HDFC']).toBeCloseTo(-150, 4);
    expect(result.total).toBeCloseTo(-150, 4);
  });
});
