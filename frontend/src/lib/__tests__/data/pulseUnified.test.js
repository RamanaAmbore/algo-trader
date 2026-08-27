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
import { mergePositionRows, makeRowFactory } from '../../data/pulseUnified.js';
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
    day_change_val:      300,   // brokerDcv — what closed-hours path should return
    overnight_quantity:  50,
    realised:            0,
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
  it('marketOpen=false: day_pnl uses stable brokerDcv, not live LTP', () => {
    const byKey = {};
    const row = makeOvernightPositionRow();
    // brokerDcv = baseDayPnlForPosition(row). The row has overnight_quantity=50
    // and day_change_val=300 — so brokerDcv = 300 (fast-path).
    const expectedBrokerDcv = baseDayPnlForPosition(row);
    expect(expectedBrokerDcv).toBe(300);

    // Provide a live SSE LTP in the snap so the live-LTP path WOULD fire if
    // marketOpen were true. liveLtp=140 → live path = (140−125)×50 = 750.
    const snap = { ltp: 140 };
    const ctx = makeCtx(false, { NIFTY25AUG24000CE: snap });

    mergePositionRows(byKey, [row], true, {}, ctx);

    const result = byKey['NIFTY25AUG24000CE__pos'];
    expect(result).toBeDefined();
    // Closed hours must NOT use live LTP (750); must use brokerDcv (300).
    expect(result.day_pnl).toBe(300);
    expect(result.day_pnl).not.toBe(750);
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
    const second = run(6350);  // different LTP — simulates next SSE tick

    // Both runs must produce the same brokerDcv (300), not different live values.
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
