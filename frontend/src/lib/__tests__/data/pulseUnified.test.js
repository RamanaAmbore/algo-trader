/**
 * pulseUnified.test.js
 *
 * Tests for the `marketOpen` gate fix in mergePositionRows.
 *
 * Root cause: `livePositionDayPnl` was called with `marketOpen: true` hardcoded
 * instead of `isMarketOpen()`. In closed hours, SSE LTP ticks still arrive for
 * MCX / pre-market symbols and kept shifting `row.day_pnl` on each buildUnified
 * call, causing visible ag-Grid re-render animations.
 *
 * Fix: `mergePositionRows` now destructures `isMarketOpen` from the ctx bag and
 * calls it, so closed-hours callers that pass `isMarketOpen: () => false` get
 * the stable `brokerDcv` fallback path.
 *
 * Five quality dimensions:
 *  1. SSOT   — marketOpen gate is the single branch in livePositionDayPnl that
 *              controls live-LTP vs brokerDcv; this test verifies the gate is
 *              exercised from mergePositionRows via ctx.isMarketOpen.
 *  2. Perf   — pure unit test, no DOM / network, sub-millisecond.
 *  3. Stale  — confirms the old hardcoded `true` path is unreachable when
 *              `isMarketOpen: () => false` is supplied.
 *  4. Reuse  — uses exported mergePositionRows + real livePositionDayPnl /
 *              baseDayPnlForPosition from nav.js (no mocked logic copies).
 *  5. UX     — day_pnl stability in closed hours prevents the Day% cell
 *              refresh animation that triggered this fix.
 */

import { describe, it, expect } from 'vitest';
import { mergePositionRows, mergeHoldingRows, makeRowFactory } from '../../data/pulseUnified.js';
import { baseDayPnlForPosition, livePositionDayPnl } from '$lib/data/nav.js';

// ── Shared test fixtures ──────────────────────────────────────────────────────

/**
 * Minimal position broker row that has a valid overnight position with a
 * known day_change_val (brokerDcv) and a close price, so we can distinguish
 * the live-LTP path from the brokerDcv fallback path.
 */
function makeOvernightPositionRow(overrides = {}) {
  return {
    tradingsymbol:       'NIFTY25AUG24000CE',
    exchange:            'NFO',
    quantity:            50,
    average_price:       120,
    last_price:          130,
    previous_close:      125,   // close > 0 → live path is (live - 125) * 50
    close_price:         125,
    pnl:                 500,
    day_change_val:      300,   // vestigial — no longer read by the formula
    overnight_quantity:  50,
    realised:            0,
    prev_settlement_pnl: 200,   // pnl(500) - day_change_val(300) → base=300 (brokerDcv-equivalent)
    ...overrides,
  };
}

/**
 * Build the ctx bag for mergePositionRows.
 *
 * @param {boolean|(() => boolean)} marketOpen
 * @param {Record<string, any>} snapMap  symbol → snap object
 */
function makeCtx(marketOpen, snapMap = {}) {
  const isMarketOpenFn = typeof marketOpen === 'function'
    ? marketOpen
    : () => marketOpen;
  return {
    snapOf: (sym) => snapMap[sym] ?? null,
    getInst: null,
    isMarketOpen: isMarketOpenFn,
    baseDayPnlForPosition,
    livePositionDayPnl,
  };
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('mergePositionRows — marketOpen gate (Fix B)', () => {
  it('marketOpen=false: day_pnl returns the stable base, not live SSE LTP', () => {
    const byKey = {};
    const row = makeOvernightPositionRow();
    // Confirm base (baseDayPnlForPosition) = 300 so we can assert it IS used.
    const expectedBase = baseDayPnlForPosition(row);
    expect(expectedBase).toBe(300);

    // Provide a live SSE LTP in the snap so the live-LTP delta WOULD fire if
    // marketOpen were true. liveLtp=140, pollLtp=130 → delta = (140-130)*50 = 500.
    const snap = { ltp: 140 };
    const ctx = makeCtx(false, { NIFTY25AUG24000CE: snap });

    mergePositionRows(byKey, [row], true, {}, ctx);

    const result = byKey['NIFTY25AUG24000CE__pos'];
    expect(result).toBeDefined();
    // Closed hours → live delta gated off entirely → returns base=300.
    // Must NOT apply the live delta (which would give 300+500=800).
    expect(result.day_pnl).toBe(300);
    expect(result.day_pnl).not.toBe(800);
  });

  it('marketOpen=true: day_pnl uses live LTP when snap has ltp > 0', () => {
    const byKey = {};
    const row = makeOvernightPositionRow();

    // liveLtp=140, closePx=125, qty=50
    // livePositionDayPnl live path: realisedToday + (live - close) * qty
    // pollLtp=130 → realisedToday = brokerDcv - (pollLtp - close) * qty
    //             = 300 - (130 - 125) * 50 = 300 - 250 = 50
    // live result = 50 + (140 - 125) * 50 = 50 + 750 = 800
    const snap = { ltp: 140 };
    const ctx = makeCtx(true, { NIFTY25AUG24000CE: snap });

    mergePositionRows(byKey, [row], true, {}, ctx);

    const result = byKey['NIFTY25AUG24000CE__pos'];
    expect(result).toBeDefined();
    // Should be 800, not brokerDcv (300).
    expect(result.day_pnl).toBe(800);
    expect(result.day_pnl).not.toBe(300);
  });

  it('marketOpen=false: day_pnl is stable across two buildUnified calls with different MCX LTPs', () => {
    // Simulates SSE ticks arriving in closed hours — day_pnl must not shift.
    const row = makeOvernightPositionRow({ tradingsymbol: 'CRUDEOIL25AUGFUT', exchange: 'MCX' });

    const run = (ltp) => {
      const byKey = {};
      const ctx = makeCtx(false, { CRUDEOIL25AUGFUT: { ltp } });
      mergePositionRows(byKey, [row], true, {}, ctx);
      return byKey['CRUDEOIL25AUGFUT__pos'].day_pnl;
    };

    const first  = run(6200);
    const second = run(6350);  // different SSE LTP — simulates next tick

    // Both runs return the stable base=300 — closed hours gates the live
    // delta off entirely, so different SSE LTPs never shift the result.
    expect(first).toBe(300);
    expect(second).toBe(300);
    expect(first).toBe(second);
  });

  it('marketOpen=true with no snap ltp: falls back to brokerDcv (posLiveLtp=null)', () => {
    const byKey = {};
    const row = makeOvernightPositionRow();
    // No snap and no liveQ — posLiveLtp resolves to null.
    const ctx = makeCtx(true, {});

    mergePositionRows(byKey, [row], true, {}, ctx);

    const result = byKey['NIFTY25AUG24000CE__pos'];
    // With live=null, livePositionDayPnl returns brokerDcv.
    expect(result.day_pnl).toBe(300);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// mergeHoldingRows — holdClose guard (holdClose <= 0)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Build a minimal holdings broker row.
 * @param {Partial<any>} overrides
 * @returns {any}
 */
function makeHoldingRow(overrides = {}) {
  return {
    tradingsymbol:   'TCS',
    exchange:        'NSE',
    symbol:          'TCS',
    quantity:        100,
    average_price:   2500,
    last_price:      2700,
    previous_close:  2650,   // Default to > 0 — can be overridden
    close_price:     2650,   // Will be ignored per the fix
    pnl:             20000,
    day_change_val:  500,    // dcv — fallback when holdClose <= 0
    ...overrides,
  };
}

/**
 * Build ctx for mergeHoldingRows.
 * @param {Record<string, any>} snapMap  symbol → snap object
 * @returns {any}
 */
function makeHoldingCtx(snapMap = {}) {
  return {
    snapOf: (sym) => snapMap[sym] ?? null,
    getInst: null,
    isMarketOpen: () => true, // Standard — holdings snapshot logic doesn't gate on market open
  };
}

describe('mergeHoldingRows — holdClose guard', () => {
  it('holdClose < 0: uses dcv (guard fires for negative previous_close)', () => {
    const byKey = {};
    const holdingRow = makeHoldingRow({ previous_close: -1, day_change_val: 500 });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // holdClose = -1 → guard fires → uses day_change_val = 500
    expect(result.day_pnl).toBe(500);
  });

  it('holdClose = 0: uses dcv', () => {
    const byKey = {};
    const holdingRow = makeHoldingRow({ previous_close: 0, close_price: 0, day_change_val: 400 });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // holdClose = 0 → guard fires → uses day_change_val = 400
    expect(result.day_pnl).toBe(400);
  });

  it('holdClose > 0 with valid ltp: uses (ltp - holdClose) * qty when epsilon check passes', () => {
    const byKey = {};
    // previous_close: 2650, ltp: 2700, qty: 100
    // day_pnl = (2700 - 2650) * 100 = 5000
    const holdingRow = makeHoldingRow({
      previous_close: 2650,
      last_price: 2700,
      quantity: 100,
      day_change_val: 0, // Not used
    });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // holdClose = 2650 > 0, epsilon check: |2700 - 2650| = 50 > 0.005 → passes
    // day_pnl = (2700 - 2650) * 100 = 5000
    expect(result.day_pnl).toBe(5000);
  });

  it('close_price is ignored for holdClose: only previous_close is used', () => {
    const byKey = {};
    // previous_close: 0 (should fire guard), close_price: 2650 (should be ignored)
    const holdingRow = makeHoldingRow({
      previous_close: 0,
      close_price: 2650,
      day_change_val: 350,
    });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // Old buggy code would compute: (2700 - 2650) * 100 = 5000
    // Fixed code: holdClose = Number(previous_close) || Number(close_price) = 0
    // Guard fires → uses day_change_val = 350
    expect(result.day_pnl).toBe(350);
    expect(result.day_pnl).not.toBe(5000); // Not the close_price formula result
  });

  it('holdClose > 0 but epsilon check fails: uses dcv', () => {
    const byKey = {};
    // previous_close: 2650.002, ltp: 2650 (post-settlement, within 0.005 epsilon)
    // |2650 - 2650.002| = 0.002 ≤ 0.005 → epsilon check fails → uses dcv
    const holdingRow = makeHoldingRow({
      previous_close: 2650.002,
      last_price: 2650,
      day_change_val: 600,
    });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // epsilon check fails → fallback to dcv
    expect(result.day_pnl).toBe(600);
  });

  it('holdClose = holdAvg: uses dcv (Guard 2 — lifetime vs day P&L)', () => {
    const byKey = {};
    // average_price: 2500, previous_close: 2500 (same)
    // Guard 2 fires → uses dcv, not lifetime formula
    const holdingRow = makeHoldingRow({
      average_price: 2500,
      previous_close: 2500,
      last_price: 2700,
      quantity: 100,
      day_change_val: 450,
    });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // holdClose = 2500 = holdAvg → Guard 2 fires → uses dcv = 450
    expect(result.day_pnl).toBe(450);
  });

  it('quantity = 0 (closed holding): day_pnl still uses proper formula', () => {
    const byKey = {};
    const holdingRow = makeHoldingRow({
      quantity: 0,
      previous_close: 2650,
      last_price: 2700,
      day_change_val: 0,
    });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // qty = 0 → (2700 - 2650) * 0 = 0
    expect(result.day_pnl).toBe(0);
  });

  it('snap ltp overrides last_price for day_pnl calculation', () => {
    const byKey = {};
    const holdingRow = makeHoldingRow({
      previous_close: 2600,
      last_price: 2700, // Ignored when snap has ltp
      quantity: 100,
    });
    // snap.ltp: 2750 (live tick overrides last_price)
    const ctx = makeHoldingCtx({ TCS: { ltp: 2750 } });

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // liveHold = 2750 (from snap), holdClose = 2600
    // day_pnl = (2750 - 2600) * 100 = 15000
    expect(result.day_pnl).toBe(15000);
  });
});
