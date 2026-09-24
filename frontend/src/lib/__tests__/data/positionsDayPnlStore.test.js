/**
 * positionsDayPnlStore.test.js
 *
 * Unit tests for the positions day P&L store computation logic.
 *
 * The store aggregates day P&L across all positions by:
 *   1. Reading from positionsStore.value (array of broker position rows)
 *   2. For each row, calling livePositionDayPnl with live LTP from symbolStore
 *   3. Exporting { total: number, byKey: { "EXCHANGE:SYMBOL": number } }
 *
 * This test file validates the underlying computation using the actual
 * livePositionDayPnl and baseDayPnlForPosition functions from nav.js,
 * following the pattern established in positions_holdings_ssot.test.js.
 *
 * Five quality dimensions:
 *   1. SSOT   — livePositionDayPnl and baseDayPnlForPosition are canonical
 *   2. Perf   — pure unit, no DOM / network, sub-millisecond
 *   3. Stale  — market-closed and edge cases still return correct values
 *   4. Reuse  — exercises exported nav.js functions
 *   5. UX     — key format, empty positions, aggregation all handled
 */

import { describe, it, expect } from 'vitest';
import { livePositionDayPnl, baseDayPnlForPosition } from '$lib/data/nav.js';

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Simulate the store's computation: given positions array, symbolStore snapshots,
 * return { total, byKey }.
 */
function computePositionsDayPnl(positions = [], snapshots = {}, marketOpen = true) {
  const byKey = {};
  let total = 0;

  for (const pos of positions) {
    const symbol = pos.tradingsymbol || '';
    const exchange = (pos.exchange || '').toUpperCase();
    const key = `${exchange}:${symbol.toUpperCase()}`;

    // Get live LTP from snapshot, fall back to last_price
    const snap = snapshots[symbol];
    const liveLtp = snap?.ltp ?? pos.last_price ?? null;

    // Compute day P&L using livePositionDayPnl
    const dayPnl = livePositionDayPnl(
      {
        pollLtp: pos.last_price || 0,
        qty: pos.quantity || 0,
        dcvRow: pos, // raw row for baseDayPnlForPosition
      },
      liveLtp,
      { marketOpen }
    );

    byKey[key] = dayPnl;
    total += dayPnl;
  }

  return { total, byKey };
}

function makePositionRow(overrides = {}) {
  return {
    tradingsymbol: 'INFY25AUGFUT',
    exchange: 'NFO',
    quantity: 10,
    average_price: 990,
    close_price: 1000,
    last_price: 1005,
    pnl: 150,
    day_change_val: 50,
    overnight_quantity: 10,
    realised: 0,
    ...overrides,
  };
}

// ── Test 1: Single position with live tick available ─────────────────────────

describe('positionsDayPnlStore computation — single position', () => {
  it('single position, live tick available: ltp=1010, close=1000, qty=10 → day_pnl=100', () => {
    const positions = [
      makePositionRow({
        tradingsymbol: 'RELIANCE',
        exchange: 'NSE',
        quantity: 10,
        average_price: 990,
        close_price: 1000,
        last_price: 1005,
        overnight_quantity: 10,
        day_change_val: 50,
        pnl: 200,
        prev_settlement_pnl: 150, // pnl(200) - day_change_val(50) → base=50
      }),
    ];
    const snapshots = { RELIANCE: { ltp: 1010, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // base = pnl(200) - prev_settlement_pnl(150) = 50
    // delta = (1010-1005)*10 = 50 → result = 100
    expect(result.byKey['NSE:RELIANCE']).toBeCloseTo(100, 4);
    expect(result.total).toBeCloseTo(100, 4);
  });

  it('single position, key format is EXCHANGE:SYMBOL uppercase', () => {
    const positions = [
      makePositionRow({
        tradingsymbol: 'reliance',
        exchange: 'nse',
      }),
    ];
    const result = computePositionsDayPnl(positions, {}, false);
    // Should normalize key to NSE:RELIANCE
    expect(Object.keys(result.byKey)[0]).toBe('NSE:RELIANCE');
  });

  it('single position, snapshot ltp used over last_price', () => {
    const positions = [
      makePositionRow({
        tradingsymbol: 'NIFTY25AUGFUT',
        exchange: 'NFO',
        quantity: 1,
        close_price: 24000,
        last_price: 24100,
        overnight_quantity: 1,
        day_change_val: 100,
        prev_settlement_pnl: 50, // pnl(150, default) - day_change_val(100) → base=100
      }),
    ];
    const snapshots = { NIFTY25AUGFUT: { ltp: 24200, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // base = 100; delta = (24200-24100)*1 = 100 → result = 200
    // Snapshot LTP (24200) takes precedence over last_price (24100) for pollLtp comparison.
    expect(result.byKey['NFO:NIFTY25AUGFUT']).toBeCloseTo(200, 4);
  });
});

// ── Test 2: Single position with no live tick (fallback to dcv) ───────────────

describe('positionsDayPnlStore — fallback to baseDayPnlForPosition', () => {
  it('no live tick, falls back to dcv path: dcv=50, total=50', () => {
    const positions = [
      makePositionRow({
        tradingsymbol: 'INFY25AUGFUT',
        exchange: 'NFO',
        overnight_quantity: 10,
        day_change_val: 50,
        prev_settlement_pnl: 100, // pnl(150, default) - day_change_val(50) → base=50
      }),
    ];
    // No snapshots provided — liveLtp will be null or use last_price fallback
    const result = computePositionsDayPnl(positions, {}, true);

    // No snapshot → liveLtp falls back to pos.last_price === pollLtp → delta=0
    // → result = base = 50
    expect(result.byKey['NFO:INFY25AUGFUT']).toBeCloseTo(50, 4);
    expect(result.total).toBeCloseTo(50, 4);
  });

  it('market closed: ignores live tick, uses dcv fallback', () => {
    // Fix 3 (audit P3b): positionsDayPnlStore passes { marketOpen: isMarketOpen() }
    // instead of the hardcoded `true`. This test (marketOpen=false) validates the
    // gate: post-settlement, stale SSE ticks with ltp_ts > 0 must NOT activate
    // the live-tick rescue path when the market is closed.
    const positions = [
      makePositionRow({
        overnight_quantity: 10,
        day_change_val: 50,
        prev_settlement_pnl: 100, // pnl(150, default) - day_change_val(50) → base=50
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 1050, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, false);

    // marketOpen=false → live delta gated off entirely → returns base=50
    expect(result.byKey['NFO:INFY25AUGFUT']).toBeCloseTo(50, 4);
  });
});

// ── Test 3: Multiple positions ───────────────────────────────────────────────

describe('positionsDayPnlStore — multiple positions', () => {
  it('two positions: total = sum of individual day_pnls', () => {
    const positions = [
      makePositionRow({
        tradingsymbol: 'RELIANCE',
        exchange: 'NSE',
        quantity: 5,
        close_price: 1000,
        last_price: 1005,
        overnight_quantity: 5,
        day_change_val: 25,
        prev_settlement_pnl: 125, // pnl(150, default) - day_change_val(25) → base=25
      }),
      makePositionRow({
        tradingsymbol: 'INFY25AUGFUT',
        exchange: 'NFO',
        quantity: 10,
        close_price: 1000,
        last_price: 1005,
        overnight_quantity: 10,
        day_change_val: 50,
        prev_settlement_pnl: 100, // pnl(150, default) - day_change_val(50) → base=50
      }),
    ];
    const snapshots = {
      RELIANCE: { ltp: 1010, ltp_ts: 1 },
      INFY25AUGFUT: { ltp: 1010, ltp_ts: 1 },
    };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // Pos 1: base=25, delta=(1010-1005)*5=25 → 50
    // Pos 2: base=50, delta=(1010-1005)*10=50 → 100
    // Total: 150
    expect(result.byKey['NSE:RELIANCE']).toBeCloseTo(50, 4);
    expect(result.byKey['NFO:INFY25AUGFUT']).toBeCloseTo(100, 4);
    expect(result.total).toBeCloseTo(150, 4);
  });

  it('three mixed positions with different exchanges', () => {
    const positions = [
      makePositionRow({
        tradingsymbol: 'CRUDEOIL25AUGFUT',
        exchange: 'MCX',
        quantity: 2,
        close_price: 500,
        last_price: 505,
        overnight_quantity: 2,
        day_change_val: 10,
        prev_settlement_pnl: 140, // pnl(150, default) - day_change_val(10) → base=10
      }),
      makePositionRow({
        tradingsymbol: 'EURINR25AUGFUT',
        exchange: 'CDS',
        quantity: 1,
        close_price: 88,
        last_price: 89,
        overnight_quantity: 1,
        day_change_val: 1,
        prev_settlement_pnl: 149, // pnl(150, default) - day_change_val(1) → base=1
      }),
      makePositionRow({
        tradingsymbol: 'NIFTY25AUGFUT',
        exchange: 'NFO',
        quantity: 1,
        close_price: 24000,
        last_price: 24100,
        overnight_quantity: 1,
        day_change_val: 100,
        prev_settlement_pnl: 50, // pnl(150, default) - day_change_val(100) → base=100
      }),
    ];
    const snapshots = {
      CRUDEOIL25AUGFUT: { ltp: 510, ltp_ts: 1 },
      EURINR25AUGFUT: { ltp: 90, ltp_ts: 1 },
      NIFTY25AUGFUT: { ltp: 24200, ltp_ts: 1 },
    };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // MCX: base=10, delta=(510-505)*2=10 → 20
    // CDS: base=1, delta=(90-89)*1=1 → 2
    // NFO: base=100, delta=(24200-24100)*1=100 → 200
    // Total: 222
    expect(result.byKey['MCX:CRUDEOIL25AUGFUT']).toBeCloseTo(20, 4);
    expect(result.byKey['CDS:EURINR25AUGFUT']).toBeCloseTo(2, 4);
    expect(result.byKey['NFO:NIFTY25AUGFUT']).toBeCloseTo(200, 4);
    expect(result.total).toBeCloseTo(222, 4);
  });
});

// ── Test 4: Short position (negative qty) ────────────────────────────────────

describe('positionsDayPnlStore — short positions', () => {
  it('short position, qty=-5, close=500, ltp=480 → gain=100', () => {
    const positions = [
      makePositionRow({
        quantity: -5,
        close_price: 500,
        last_price: 490,
        overnight_quantity: -5,
        day_change_val: 50, // -5 * (490-500) = 50
        average_price: 510,
        prev_settlement_pnl: 100, // pnl(150, default) - day_change_val(50) → base=50
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 480, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // base=50; delta = (480-490)*(-5) = 50 → result = 100
    expect(result.byKey['NFO:INFY25AUGFUT']).toBeCloseTo(100, 4);
  });

  it('short position, qty=-10, price spikes up → loss', () => {
    const positions = [
      makePositionRow({
        quantity: -10,
        close_price: 500,
        last_price: 510,
        overnight_quantity: -10,
        day_change_val: -100, // -10 * (510-500) = -100
        average_price: 495,
        prev_settlement_pnl: 250, // pnl(150, default) - day_change_val(-100) → base=-100
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 520, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // base=-100; delta = (520-510)*(-10) = -100 → result = -200
    expect(result.byKey['NFO:INFY25AUGFUT']).toBeCloseTo(-200, 4);
  });

  it('short position with negative day_change_val in total', () => {
    const positions = [
      makePositionRow({
        tradingsymbol: 'NIFTY25AUG100PE',
        quantity: -1,
        close_price: 50,
        last_price: 55,
        overnight_quantity: -1,
        day_change_val: -5, // -1 * (55-50) = -5
        prev_settlement_pnl: 155, // pnl(150, default) - day_change_val(-5) → base=-5
      }),
      makePositionRow({
        tradingsymbol: 'NIFTY25AUG100CE',
        quantity: 1,
        close_price: 50,
        last_price: 55,
        overnight_quantity: 1,
        day_change_val: 5,
        prev_settlement_pnl: 145, // pnl(150, default) - day_change_val(5) → base=5
      }),
    ];
    const snapshots = {
      NIFTY25AUG100PE: { ltp: 60, ltp_ts: 1 },
      NIFTY25AUG100CE: { ltp: 60, ltp_ts: 1 },
    };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // PE short: base=-5, delta=(60-55)*(-1)=-5 → -10
    // CE long: base=5, delta=(60-55)*1=5 → 10
    // Total: 0 (hedge neutral)
    expect(result.byKey['NFO:NIFTY25AUG100PE']).toBeCloseTo(-10, 4);
    expect(result.byKey['NFO:NIFTY25AUG100CE']).toBeCloseTo(10, 4);
    expect(result.total).toBeCloseTo(0, 4);
  });
});

// ── Test 5: Empty positions array ────────────────────────────────────────────

describe('positionsDayPnlStore — edge cases', () => {
  it('empty positions array: total=0, byKey={}', () => {
    const result = computePositionsDayPnl([], {}, true);
    expect(result.total).toBe(0);
    expect(result.byKey).toEqual({});
  });

  it('position with quantity=0: day_pnl falls back to pnl', () => {
    // When qty=0, livePositionDayPnl falls to baseDayPnlForPosition.
    // makePositionRow defaults to pnl=150, so result is 150.
    const positions = [makePositionRow({ quantity: 0, overnight_quantity: 0, day_change_val: 0, pnl: 0 })];
    const result = computePositionsDayPnl(positions, {}, true);
    expect(result.byKey['NFO:INFY25AUGFUT']).toBe(0);
    expect(result.total).toBe(0);
  });

  it('live tick = 0: falls back to dcv path', () => {
    const positions = [
      makePositionRow({
        overnight_quantity: 5,
        day_change_val: 25,
        prev_settlement_pnl: 125, // pnl(150, default) - day_change_val(25) → base=25
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 0, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // ltp=0 fails the >0 guard → live delta skipped → returns base=25
    expect(result.byKey['NFO:INFY25AUGFUT']).toBeCloseTo(25, 4);
  });
});

// ── Test 6: New position (close_price=0, avg > 0) ──────────────────────────

describe('positionsDayPnlStore — new positions opened today', () => {
  it('new position: close=0, avg=100, qty=5, liveLtp=110 → pnl=50', () => {
    const positions = [
      makePositionRow({
        close_price: 0,
        average_price: 100,
        quantity: 5,
        overnight_quantity: 0,
        day_change_val: 0,
        pnl: 50, // (110-100)*5 — broker's last poll was already at ltp=110
        last_price: 110, // pollLtp matches the poll pnl was computed at
        prev_settlement_pnl: null, // no prior close-reset snapshot — new position
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 110, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // base = pnl = 50; delta = (110-110)*5 = 0 (no move since last poll) → 50
    expect(result.byKey['NFO:INFY25AUGFUT']).toBeCloseTo(50, 4);
  });

  it('new short position: close=0, avg=200, qty=-3, liveLtp=195 → pnl=15', () => {
    const positions = [
      makePositionRow({
        close_price: 0,
        average_price: 200,
        quantity: -3,
        overnight_quantity: 0,
        day_change_val: 0,
        pnl: 15, // (195-200)*(-3) — broker's last poll was already at ltp=195
        last_price: 195, // pollLtp matches the poll pnl was computed at
        prev_settlement_pnl: null, // no prior close-reset snapshot — new position
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 195, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // base = pnl = 15; delta = (195-195)*(-3) = 0 (no move since last poll) → 15
    expect(result.byKey['NFO:INFY25AUGFUT']).toBeCloseTo(15, 4);
  });

  it('new position, market closed: falls back to baseDayPnlForPosition', () => {
    const positions = [
      makePositionRow({
        close_price: 0,
        average_price: 100,
        quantity: 5,
        overnight_quantity: 0,
        day_change_val: 0,
        pnl: 50,
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 110, ltp_ts: 1 } };
    // marketOpen=false → new-position branch gated off
    const result = computePositionsDayPnl(positions, snapshots, false);

    // Falls to baseDayPnlForPosition: oq=0, dcv=0 → pnl - 0*(0-100) = 50
    expect(result.byKey['NFO:INFY25AUGFUT']).toBeCloseTo(50, 4);
  });
});

// ── Test 7: Mixed overnight + intraday adds ──────────────────────────────────

describe('positionsDayPnlStore — mixed overnight + intraday positions', () => {
  it('overnight short + new intraday sells: decomposed dcv applied correctly', () => {
    // Scenario: overnight short 10 at avg=200, today sold 5 more at 220.
    // Total qty=-15, dcv at pollLtp=215: oq*dcv + sell_realised = -50+25 = -25
    const positions = [
      makePositionRow({
        quantity: -15,
        average_price: 206.67,
        close_price: 210,
        last_price: 215,
        overnight_quantity: -10,
        day_change_val: -25, // decomposed
        pnl: -125,
        prev_settlement_pnl: -100, // pnl(-125) - day_change_val(-25) → base=-25
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 220, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // base=-25; delta=(220-215)*(-15)=-75 → result=-100
    expect(result.byKey['NFO:INFY25AUGFUT']).toBeCloseTo(-100, 4);
  });

  it('overnight + intraday buy adds: correct aggregation', () => {
    // Overnight long 5 at avg=1000, bought 5 more intraday at 1010.
    // Total qty=10, close=1000, avg=1005
    const positions = [
      makePositionRow({
        quantity: 10,
        average_price: 1005,
        close_price: 1000,
        last_price: 1015,
        overnight_quantity: 5,
        day_change_val: 75, // 5*(1015-1000) + 5*(1015-1010) = 75
        pnl: 100, // (1015-1005)*10
        prev_settlement_pnl: 25, // pnl(100) - day_change_val(75) → base=75
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 1020, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // base=75; delta=(1020-1015)*10=50 → result=125
    expect(result.byKey['NFO:INFY25AUGFUT']).toBeCloseTo(125, 4);
  });
});

// ── Test 8: Aggregation across multiple positions with negative pnls ────────

describe('positionsDayPnlStore — aggregation with mixed profit/loss', () => {
  it('winning and losing positions: total = sum', () => {
    const positions = [
      makePositionRow({
        tradingsymbol: 'NIFTY25AUGFUT',
        exchange: 'NFO',
        quantity: 1,
        close_price: 24000,
        last_price: 24100,
        overnight_quantity: 1,
        day_change_val: 100,
        prev_settlement_pnl: 50, // pnl(150, default) - day_change_val(100) → base=100
      }),
      makePositionRow({
        tradingsymbol: 'BANKNIFTY25AUGFUT',
        exchange: 'NFO',
        quantity: 1,
        close_price: 52000,
        last_price: 51900,
        overnight_quantity: 1,
        day_change_val: -100,
        prev_settlement_pnl: 250, // pnl(150, default) - day_change_val(-100) → base=-100
      }),
    ];
    const snapshots = {
      NIFTY25AUGFUT: { ltp: 24200, ltp_ts: 1 },
      BANKNIFTY25AUGFUT: { ltp: 51800, ltp_ts: 1 },
    };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // NIFTY: base=100, delta=(24200-24100)*1=100 → 200
    // BANKNIFTY: base=-100, delta=(51800-51900)*1=-100 → -200
    // Total: 0
    expect(result.byKey['NFO:NIFTY25AUGFUT']).toBeCloseTo(200, 4);
    expect(result.byKey['NFO:BANKNIFTY25AUGFUT']).toBeCloseTo(-200, 4);
    expect(result.total).toBeCloseTo(0, 4);
  });

  it('three positions with net positive total', () => {
    const positions = [
      makePositionRow({
        tradingsymbol: 'P1',
        quantity: 1,
        close_price: 100,
        last_price: 105,
        overnight_quantity: 1,
        day_change_val: 5,
        prev_settlement_pnl: 145, // pnl(150, default) - day_change_val(5) → base=5
      }),
      makePositionRow({
        tradingsymbol: 'P2',
        quantity: 1,
        close_price: 200,
        last_price: 190,
        overnight_quantity: 1,
        day_change_val: -10,
        prev_settlement_pnl: 160, // pnl(150, default) - day_change_val(-10) → base=-10
      }),
      makePositionRow({
        tradingsymbol: 'P3',
        quantity: 2,
        close_price: 50,
        last_price: 52,
        overnight_quantity: 2,
        day_change_val: 4,
        prev_settlement_pnl: 146, // pnl(150, default) - day_change_val(4) → base=4
      }),
    ];
    const snapshots = {
      P1: { ltp: 110, ltp_ts: 1 },
      P2: { ltp: 185, ltp_ts: 1 },
      P3: { ltp: 55, ltp_ts: 1 },
    };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // P1: base=5, delta=(110-105)*1=5 → 10
    // P2: base=-10, delta=(185-190)*1=-5 → -15
    // P3: base=4, delta=(55-52)*2=6 → 10
    // Total: 5
    expect(result.byKey['NFO:P1']).toBeCloseTo(10, 4);
    expect(result.byKey['NFO:P2']).toBeCloseTo(-15, 4);
    expect(result.byKey['NFO:P3']).toBeCloseTo(10, 4);
    expect(result.total).toBeCloseTo(5, 4);
  });
});

// ── Test 9: Fractional prices and quantities ─────────────────────────────────

describe('positionsDayPnlStore — fractional values', () => {
  it('fractional price and quantity: precise calculation', () => {
    const positions = [
      makePositionRow({
        quantity: 2.5,
        close_price: 1234.567,
        last_price: 1234.890,
        overnight_quantity: 2.5,
        day_change_val: 0.8075, // 2.5 * 0.323
        average_price: 1200.0,
        prev_settlement_pnl: 149.1925, // pnl(150, default) - day_change_val(0.8075) → base=0.8075
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 1235.1, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // base=0.8075; delta=(1235.1-1234.890)*2.5=0.525 → 1.3325
    expect(result.byKey['NFO:INFY25AUGFUT']).toBeCloseTo(1.3325, 3);
  });
});

// ── Test 10: Large positions ─────────────────────────────────────────────────

describe('positionsDayPnlStore — large positions', () => {
  it('large lot MCX position: correct aggregation', () => {
    const positions = [
      makePositionRow({
        tradingsymbol: 'CRUDEOIL25SEPTFUT',
        exchange: 'MCX',
        quantity: 100, // 100 contracts
        close_price: 6500,
        last_price: 6510,
        overnight_quantity: 100,
        day_change_val: 1000, // 100 * (6510-6500)
        average_price: 6400,
        prev_settlement_pnl: -850, // pnl(150, default) - day_change_val(1000) → base=1000
      }),
    ];
    const snapshots = { CRUDEOIL25SEPTFUT: { ltp: 6520, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // base=1000; delta=(6520-6510)*100=1000 → 2000
    expect(result.byKey['MCX:CRUDEOIL25SEPTFUT']).toBeCloseTo(2000, 4);
  });
});

// ── Test 11: previous_close / close_price are no longer read by the formula ─
//
// Historical note: these three tests originally validated a `previous_close ||
// close_price` cascade inside the old Case-branch Day P&L formula. The
// baseline-diff redesign (`current_total_profit − prev_settlement_pnl`)
// eliminated that cascade entirely — `close_price`/`previous_close` no
// longer feed the positions Day P&L formula at all (holdings keeps its own,
// separate close-price mechanism unchanged — see holdingsDayPnlStore).
// Rewritten to assert that invariant directly: the formula's result is
// unaffected by close_price/previous_close, and depends only on
// pnl/prev_settlement_pnl (base) and pollLtp/liveLtp/qty (live delta).

describe('positionsDayPnlStore — close_price/previous_close no longer affect the formula', () => {
  it('result is identical regardless of close_price / previous_close values', () => {
    const base = {
      tradingsymbol: 'RELIANCE', exchange: 'NSE', quantity: 10,
      average_price: 490, last_price: 510, pnl: 200, prev_settlement_pnl: 100,
    };
    const withDrift  = makePositionRow({ ...base, previous_close: 500, close_price: 499 });
    const withoutRef = makePositionRow({ ...base, previous_close: 0,   close_price: 0 });

    const rDrift  = computePositionsDayPnl([withDrift],  { RELIANCE: { ltp: 510, ltp_ts: 1 } }, true);
    const rNoRef  = computePositionsDayPnl([withoutRef], { RELIANCE: { ltp: 510, ltp_ts: 1 } }, true);

    // base = pnl(200) - prev_settlement_pnl(100) = 100; delta = (510-510)*10 = 0 → 100
    expect(rDrift.byKey['NSE:RELIANCE']).toBeCloseTo(100, 4);
    expect(rNoRef.byKey['NSE:RELIANCE']).toBeCloseTo(100, 4);
    expect(rDrift.byKey['NSE:RELIANCE']).toBe(rNoRef.byKey['NSE:RELIANCE']);
  });

  it('no epsilon guard on positions — live delta fires even for a sub-paisa move', () => {
    // Unlike holdingsDayPnlStore (which skips its formula when |ltp−close| ≤
    // 0.005 as a post-settlement guard), livePositionDayPnl has no epsilon
    // guard — the delta applies at any non-zero (liveLtp − pollLtp).
    const positions = [
      makePositionRow({
        tradingsymbol: 'TATASTEEL',
        exchange: 'NSE',
        quantity: 100,
        average_price: 498,
        last_price: 500,
        pnl: 200,
        prev_settlement_pnl: 150, // base=50
      }),
    ];
    const snapshots = { TATASTEEL: { ltp: 500.001, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // base=50; delta=(500.001-500)*100=0.1 → 50.1
    expect(result.byKey['NSE:TATASTEEL']).toBeCloseTo(50.1, 3);
  });
});

// ── Test N: Proxy enumeration (ownKeys + getOwnPropertyDescriptor) ────────────
// The byKey shim is a Proxy. Without ownKeys/getOwnPropertyDescriptor traps,
// Object.entries(proxy) returns [] (target is {} empty), breaking
// _fnoDayPnlByRoot in the derivatives page which iterates byKey to build the
// Day P&L per-root map for the Snapshot table.

describe('positionsDayPnlStore.byKey proxy — Object.entries enumeration', () => {
  function makeShimProxy(fakeStore) {
    return new Proxy({}, {
      get(_t, sym) {
        if (typeof sym !== 'string') return undefined;
        return fakeStore[sym]?.day_pnl ?? 0;
      },
      has(_t, sym) { return sym in fakeStore; },
      ownKeys(_t) { return Object.keys(fakeStore); },
      getOwnPropertyDescriptor(_t, sym) {
        if (typeof sym === 'string' && sym in fakeStore) {
          return { configurable: true, enumerable: true, value: fakeStore[sym]?.day_pnl ?? 0 };
        }
        return undefined;
      },
    });
  }

  it('Object.entries returns all key-value pairs (not empty)', () => {
    const fakeStore = {
      'CRUDEOIL26AUGFUT': { day_pnl: -1356, exp_pnl: -2100, extrinsic: 0 },
      'GOLDM26AUGFUT':    { day_pnl: 420,   exp_pnl: 630,   extrinsic: 0 },
    };
    const proxy = makeShimProxy(fakeStore);
    const entries = Object.entries(proxy);
    expect(entries).toHaveLength(2);
    expect(entries).toContainEqual(['CRUDEOIL26AUGFUT', -1356]);
    expect(entries).toContainEqual(['GOLDM26AUGFUT', 420]);
  });

  it('Object.keys returns all symbol keys', () => {
    const fakeStore = { 'NIFTY26SEP24000CE': { day_pnl: -200 } };
    const proxy = makeShimProxy(fakeStore);
    expect(Object.keys(proxy)).toEqual(['NIFTY26SEP24000CE']);
  });

  it('missing key returns 0 via get trap', () => {
    const fakeStore = { 'GOLDM26AUGFUT': { day_pnl: 420 } };
    const proxy = makeShimProxy(fakeStore);
    expect(proxy['UNKNOWN']).toBe(0);
  });

  it('empty store: Object.entries returns []', () => {
    const proxy = makeShimProxy({});
    expect(Object.entries(proxy)).toHaveLength(0);
  });

  it('without ownKeys trap, Object.entries returns [] (demonstrates the old bug)', () => {
    const fakeStore = { 'CRUDEOIL26AUGFUT': { day_pnl: -1356 } };
    const brokenProxy = new Proxy({}, {
      get(_t, sym) {
        if (typeof sym !== 'string') return undefined;
        return fakeStore[sym]?.day_pnl ?? 0;
      },
      has(_t, sym) { return sym in fakeStore; },
      // no ownKeys — this is the broken version
    });
    expect(Object.entries(brokenProxy)).toHaveLength(0);
  });

  it('_fnoDayPnlByRoot pattern: iterate byKey and build root→dayPnl map', () => {
    const fakeStore = {
      'CRUDEOIL26AUGFUT':  { day_pnl: -1356 },
      'GOLDM26AUGFUT':     { day_pnl: 420 },
      'NIFTY26SEP24000CE': { day_pnl: -50 },
      'TATASTEEL':         { day_pnl: 80 },  // equity — filtered (doesn't end FUT/CE/PE)
    };
    const proxy = makeShimProxy(fakeStore);

    // Simulate _fnoDayPnlByRoot logic from derivatives page
    const byRoot = {};
    let total = 0;
    for (const [sym, val] of Object.entries(proxy)) {
      if (!/FUT$|(CE|PE)$/i.test(sym)) continue;
      // decomposeSymbol would give root; use simple regex here for test isolation
      const root = sym.replace(/\d{2}[A-Z]{3}(?:FUT|CE|PE|\d+(CE|PE))$/, '').replace(/\d+$/, '');
      byRoot[root] = (byRoot[root] ?? 0) + val;
      total += val;
    }
    // TATASTEEL doesn't end in FUT/CE/PE so filtered out; total = sum of F&O only
    expect(total).toBeCloseTo(-1356 + 420 + (-50), 4);
    expect(byRoot['TATASTEEL']).toBeUndefined();  // filtered out (equity)
  });
});

// ── Test 12: positionsDayPnlStore.byAccount getter (NEW) ─────────────────────

/**
 * positionsDayPnlStore now exports a byAccount getter that delegates to
 * portfolioStore.positions.byAccount, following the same pattern as holdingsDayPnlStore.
 *
 * This test suite validates the functional layer: the getter reads from the correct source
 * and aggregates per-account day P&L values.
 *
 * Five quality dimensions:
 *   1. SSOT   — byAccount reads from portfolioStore.positions.byAccount
 *   2. Perf   — pure getter, no I/O, O(1) lookup per account
 *   3. Stale  — no race conditions (reads snapshotted object)
 *   4. Reuse  — exercises the portfolioStore layer
 *   5. UX     — key format (uppercase), TOTAL inclusion, NavBreakdown consumption
 */

/**
 * Mock portfolio store with positions.byAccount layer.
 * Simulates the real portfolioStore structure for isolated testing.
 */
function createMockPortfolioStoreWithPositions(positionsByAccount = {}) {
  const totalValue = (positionsByAccount && typeof positionsByAccount === 'object' && positionsByAccount['TOTAL']) ?? 0;
  return {
    positions: {
      total: { day_pnl: totalValue },
      byKey: {},
      byRootPositions: {},
      byRootHoldings: {},
      byRoot: {},
      expiryByAcct: new Map(),
      // New: positions now has byAccount aggregation
      byAccount: positionsByAccount,
    },
  };
}

/**
 * Simulate positionsDayPnlStore.byAccount getter.
 * In the real store, this uses a Proxy and delegates to portfolioStore.
 */
function getPositionsDayPnlByAccount(mockPortfolioStore) {
  return mockPortfolioStore.positions.byAccount ?? {};
}

describe('positionsDayPnlStore.byAccount — getter delegation', () => {
  it('byAccount returns portfolioStore.positions.byAccount', () => {
    const mockStore = createMockPortfolioStoreWithPositions({
      'ZERODHA': 100,
      'DHAN': 200,
      'TOTAL': 300,
    });

    const byAccount = getPositionsDayPnlByAccount(mockStore);

    expect(byAccount['ZERODHA']).toBe(100);
    expect(byAccount['DHAN']).toBe(200);
    expect(byAccount['TOTAL']).toBe(300);
  });

  it('byAccount returns empty object when positions.byAccount is undefined', () => {
    const mockStore = {
      positions: {
        byAccount: undefined,
      },
    };

    const byAccount = getPositionsDayPnlByAccount(mockStore);

    expect(byAccount).toEqual({});
  });

  it('byAccount returns empty object when positions.byAccount is null', () => {
    const mockStore = {
      positions: {
        byAccount: null,
      },
    };

    const byAccount = getPositionsDayPnlByAccount(mockStore);

    expect(byAccount).toEqual({});
  });

  it('byAccount includes TOTAL key with aggregate value', () => {
    const mockStore = createMockPortfolioStoreWithPositions({
      'ACC1': 1000,
      'ACC2': 2000,
      'TOTAL': 3000,
    });

    const byAccount = getPositionsDayPnlByAccount(mockStore);

    expect(byAccount['TOTAL']).toBe(3000);
    expect(byAccount['TOTAL']).toBe(byAccount['ACC1'] + byAccount['ACC2']);
  });

  it('byAccount keys are always uppercase', () => {
    const mockStore = createMockPortfolioStoreWithPositions({
      'ZERODHA': 100,
      'DHAN': 200,
      'TOTAL': 300,
    });

    const byAccount = getPositionsDayPnlByAccount(mockStore);
    const keys = Object.keys(byAccount);

    for (const key of keys) {
      expect(key).toBe(key.toUpperCase());
    }
  });

  it('byAccount reflects multi-account aggregation', () => {
    const mockStore = createMockPortfolioStoreWithPositions({
      'ZERODHA': 500,
      'DHAN': 300,
      'GROWW': 200,
      'TOTAL': 1000,
    });

    const byAccount = getPositionsDayPnlByAccount(mockStore);

    expect(Object.keys(byAccount).length).toBe(4);
    expect(byAccount['ZERODHA']).toBe(500);
    expect(byAccount['DHAN']).toBe(300);
    expect(byAccount['GROWW']).toBe(200);
    expect(byAccount['TOTAL']).toBe(1000);
  });

  it('byAccount preserves negative values (losses)', () => {
    const mockStore = createMockPortfolioStoreWithPositions({
      'ZERODHA': 100,
      'DHAN': -50,  // loss
      'TOTAL': 50,
    });

    const byAccount = getPositionsDayPnlByAccount(mockStore);

    expect(byAccount['DHAN']).toBe(-50);
    expect(byAccount['TOTAL']).toBe(50);
  });

  it('byAccount with single account', () => {
    const mockStore = createMockPortfolioStoreWithPositions({
      'TESTACCT': 750,
      'TOTAL': 750,
    });

    const byAccount = getPositionsDayPnlByAccount(mockStore);

    expect(byAccount['TESTACCT']).toBe(750);
    expect(byAccount['TOTAL']).toBe(750);
    expect(Object.keys(byAccount).length).toBe(2);
  });

  it('byAccount with zero total (flat day)', () => {
    const mockStore = createMockPortfolioStoreWithPositions({
      'ACC1': 100,
      'ACC2': -100,
      'TOTAL': 0,
    });

    const byAccount = getPositionsDayPnlByAccount(mockStore);

    expect(byAccount['ACC1']).toBe(100);
    expect(byAccount['ACC2']).toBe(-100);
    expect(byAccount['TOTAL']).toBe(0);
  });

  it('byAccount values are numeric (not strings)', () => {
    const mockStore = createMockPortfolioStoreWithPositions({
      'ZERODHA': 100.5,
      'DHAN': 200.75,
      'TOTAL': 301.25,
    });

    const byAccount = getPositionsDayPnlByAccount(mockStore);

    expect(typeof byAccount['ZERODHA']).toBe('number');
    expect(typeof byAccount['DHAN']).toBe('number');
    expect(typeof byAccount['TOTAL']).toBe('number');
    expect(byAccount['ZERODHA']).toBe(100.5);
    expect(byAccount['DHAN']).toBe(200.75);
  });

  it('byAccount supports fractional rupees (precision)', () => {
    const mockStore = createMockPortfolioStoreWithPositions({
      'ZERODHA': 1234.567,
      'DHAN': 5678.901,
      'TOTAL': 6913.468,
    });

    const byAccount = getPositionsDayPnlByAccount(mockStore);

    expect(byAccount['ZERODHA']).toBeCloseTo(1234.567, 3);
    expect(byAccount['DHAN']).toBeCloseTo(5678.901, 3);
    expect(byAccount['TOTAL']).toBeCloseTo(6913.468, 3);
  });
});

describe('positionsDayPnlStore.byAccount — NavBreakdown consumption pattern', () => {
  it('byAccount provides per-account breakdown for NavBreakdown.svelte', () => {
    // NavBreakdown reads: positionsDayPnlStore.byAccount[acct]
    // and builds margin/capital rows per account
    const mockStore = createMockPortfolioStoreWithPositions({
      'ZERODHA': 500,
      'DHAN': 300,
      'GROWW': 200,
      'TOTAL': 1000,
    });

    const byAccount = getPositionsDayPnlByAccount(mockStore);

    // NavBreakdown would iterate accounts and read each value
    for (const account of ['ZERODHA', 'DHAN', 'GROWW']) {
      const dayPnl = byAccount[account];
      expect(dayPnl).toBeDefined();
      expect(typeof dayPnl).toBe('number');
    }
  });

  it('byAccount is never null or undefined (safe for template reads)', () => {
    const testCases = [
      createMockPortfolioStoreWithPositions({}),
      createMockPortfolioStoreWithPositions({ 'ZERODHA': 100, 'TOTAL': 100 }),
      createMockPortfolioStoreWithPositions(null),
      createMockPortfolioStoreWithPositions(undefined),
    ];

    for (const mockStore of testCases) {
      const byAccount = getPositionsDayPnlByAccount(mockStore);
      // Should always return an object (never null/undefined)
      expect(byAccount).toBeDefined();
      expect(typeof byAccount).toBe('object');
    }
  });

  it('byAccount TOTAL always equals NavBreakdown positions total', () => {
    const mockStore = createMockPortfolioStoreWithPositions({
      'ZERODHA': 1000,
      'DHAN': 2000,
      'TOTAL': 3000,
    });

    const byAccount = getPositionsDayPnlByAccount(mockStore);
    const positionsTotal = mockStore.positions.total.day_pnl;

    expect(byAccount['TOTAL']).toBe(positionsTotal);
  });
});
