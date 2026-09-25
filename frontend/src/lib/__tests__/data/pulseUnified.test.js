/**
 * pulseUnified.test.js
 *
 * Historical: this file originally tested the `marketOpen` gate in
 * mergePositionRows — `livePositionDayPnl` was called with `marketOpen: true`
 * hardcoded instead of `isMarketOpen()`, so in closed hours SSE LTP ticks
 * still arriving for MCX/pre-market symbols kept shifting `row.day_pnl` on
 * each buildUnified call, causing visible ag-Grid re-render animations.
 *
 * §1 (positions/holdings LTP-source redesign) removed the entire live-tick
 * mechanism: `mergePositionRows` now calls `baseDayPnlForPosition(r)`
 * directly with NO live-tick delta and no marketOpen gate — Day P&L is
 * purely poll-driven, so the closed-hours-tick-shift bug class this file
 * guarded against is structurally impossible now (there's no live-tick input
 * left to shift the result). The describe block below is rewritten as a
 * regression guard for that stronger invariant: day_pnl is IDENTICAL
 * regardless of any SSE snapshot value, open or closed market.
 *
 * Five quality dimensions:
 *  1. SSOT   — baseDayPnlForPosition is the sole source for row.day_pnl in
 *              mergePositionRows; no ctx.isMarketOpen branch remains.
 *  2. Perf   — pure unit test, no DOM / network, sub-millisecond.
 *  3. Stale  — confirms no live-tick code path exists to reintroduce the
 *              closed-hours shift bug.
 *  4. Reuse  — uses exported mergePositionRows + real baseDayPnlForPosition
 *              from nav.js (no mocked logic copies).
 *  5. UX     — day_pnl stability (open AND closed hours) prevents the Day%
 *              cell refresh animation that triggered the original fix.
 */

import { describe, it, expect } from 'vitest';
import { mergePositionRows, mergeHoldingRows, makeRowFactory } from '../../data/pulseUnified.js';
import { baseDayPnlForPosition } from '$lib/data/nav.js';

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
 * @param {Record<string, any>} snapMap  symbol → snap object
 */
function makeCtx(snapMap = {}) {
  return {
    snapOf: (sym) => snapMap[sym] ?? null,
    getInst: null,
    baseDayPnlForPosition,
  };
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('mergePositionRows — day_pnl is poll-only, no live-tick input (§1)', () => {
  it('day_pnl returns the stable base regardless of any live SSE LTP in the snap', () => {
    const byKey = {};
    const row = makeOvernightPositionRow();
    // Confirm base (baseDayPnlForPosition) = 300 so we can assert it IS used.
    const expectedBase = baseDayPnlForPosition(row);
    expect(expectedBase).toBe(300);

    // A live SSE LTP diverging from last_price (140 vs 130) has no effect —
    // there is no live-tick delta code path left in mergePositionRows.
    const snap = { ltp: 140 };
    const ctx = makeCtx({ NIFTY25AUG24000CE: snap });

    mergePositionRows(byKey, [row], true, {}, ctx);

    const result = byKey['NIFTY25AUG24000CE__pos'];
    expect(result).toBeDefined();
    expect(result.day_pnl).toBe(300);
    expect(result.day_pnl).not.toBe(800);
  });

  it('day_pnl is stable across two buildUnified calls with different MCX SSE LTPs', () => {
    // Simulates SSE ticks arriving — day_pnl must not shift either way, open
    // or closed market, since there's no live-tick input left (§1).
    const row = makeOvernightPositionRow({ tradingsymbol: 'CRUDEOIL25AUGFUT', exchange: 'MCX' });

    const run = (ltp) => {
      const byKey = {};
      const ctx = makeCtx({ CRUDEOIL25AUGFUT: { ltp } });
      mergePositionRows(byKey, [row], true, {}, ctx);
      return byKey['CRUDEOIL25AUGFUT__pos'].day_pnl;
    };

    const first  = run(6200);
    const second = run(6350);  // different SSE LTP — simulates next tick

    expect(first).toBe(300);
    expect(second).toBe(300);
    expect(first).toBe(second);
  });

  it('no snap ltp at all: still returns baseDayPnlForPosition (no live-tick fallback branch)', () => {
    const byKey = {};
    const row = makeOvernightPositionRow();
    const ctx = makeCtx({});

    mergePositionRows(byKey, [row], true, {}, ctx);

    const result = byKey['NIFTY25AUG24000CE__pos'];
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
    prev_close:      2650,   // Default to > 0 — can be overridden
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
  it('holdClose < 0: uses dcv (guard fires for negative prev_close)', () => {
    const byKey = {};
    const holdingRow = makeHoldingRow({ prev_close: -1, day_change_val: 500 });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // holdClose = -1 → guard fires → uses day_change_val = 500
    expect(result.day_pnl).toBe(500);
  });

  it('holdClose = 0: uses dcv', () => {
    const byKey = {};
    const holdingRow = makeHoldingRow({ prev_close: 0, close_price: 0, day_change_val: 400 });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // holdClose = 0 → guard fires → uses day_change_val = 400
    expect(result.day_pnl).toBe(400);
  });

  it('holdClose > 0 with valid ltp: uses (ltp - holdClose) * qty when epsilon check passes', () => {
    const byKey = {};
    // prev_close: 2650, ltp: 2700, qty: 100
    // day_pnl = (2700 - 2650) * 100 = 5000
    const holdingRow = makeHoldingRow({
      prev_close: 2650,
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

  it('close_price is ignored for holdClose: only prev_close is used', () => {
    const byKey = {};
    // prev_close: 0 (should fire guard), close_price: 2650 (should be ignored)
    const holdingRow = makeHoldingRow({
      prev_close: 0,
      close_price: 2650,
      day_change_val: 350,
    });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // Old buggy code would compute: (2700 - 2650) * 100 = 5000
    // Fixed code: holdClose = Number(prev_close) || Number(close_price) = 0
    // Guard fires → uses day_change_val = 350
    expect(result.day_pnl).toBe(350);
    expect(result.day_pnl).not.toBe(5000); // Not the close_price formula result
  });

  it('holdClose > 0 but epsilon check fails: uses dcv', () => {
    const byKey = {};
    // prev_close: 2650.002, ltp: 2650 (post-settlement, within 0.005 epsilon)
    // |2650 - 2650.002| = 0.002 ≤ 0.005 → epsilon check fails → uses dcv
    const holdingRow = makeHoldingRow({
      prev_close: 2650.002,
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
    // average_price: 2500, prev_close: 2500 (same)
    // Guard 2 fires → uses dcv, not lifetime formula
    const holdingRow = makeHoldingRow({
      average_price: 2500,
      prev_close: 2500,
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
      prev_close: 2650,
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

  it('day_pnl is poll-only: snap (SSE-tick) ltp is ignored, only last_price/liveQ feed the formula (item-8 fix)', () => {
    const byKey = {};
    const holdingRow = makeHoldingRow({
      prev_close: 2600,
      last_price: 2700, // poll-sourced — THIS drives day_pnl now
      quantity: 100,
    });
    // snap.ltp: 2750 — an SSE tick diverging from last_price. Prior to the
    // item-8 fix this drove day_pnl (liveHold preferred snap); now day_pnl
    // is poll-only (matches portfolioStore's _holdTier1 / NavStrip H —
    // §1's poll-only redesign) so the SSE tick has NO effect on day_pnl.
    const ctx = makeHoldingCtx({ TCS: { ltp: 2750 } });

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // pollHold = 2700 (last_price, NOT the 2750 snap tick), holdClose = 2600
    // day_pnl = (2700 - 2600) * 100 = 10000
    expect(result.day_pnl).toBe(10000);
    expect(result.day_pnl).not.toBe(15000); // NOT the snap-driven (2750-2600)*100 result
  });

  it('total P&L (row.pnl) still uses the live SSE tick — only day_pnl is poll-only', () => {
    const byKey = {};
    const holdingRow = makeHoldingRow({
      average_price: 2500,
      prev_close: 2600,
      last_price: 2700,
      quantity: 100,
    });
    const ctx = makeHoldingCtx({ TCS: { ltp: 2750 } });

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    // row.pnl (lifetime P&L / LTP display) is unaffected by item-8 — it
    // still prefers the live snap tick: (2750 - 2500) * 100 = 25000.
    expect(result.pnl).toBe(25000);
  });
});
