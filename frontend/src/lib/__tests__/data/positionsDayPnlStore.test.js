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
        closePx: pos.close_price || 0,
        pollLtp: pos.last_price || 0,
        qty: pos.quantity || 0,
        avg: pos.average_price || 0,
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
      }),
    ];
    const snapshots = { RELIANCE: { ltp: 1010, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // Live path fires: realisedToday = 50 - (1005-1000)*10 = 50-50 = 0
    // result = 0 + (1010-1000)*10 = 100
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
      }),
    ];
    const snapshots = { NIFTY25AUGFUT: { ltp: 24200, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // Snapshot LTP (24200) takes precedence: (24200-24000)*1 = 200
    // (not (24100-24000)*1 = 100 from last_price)
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
      }),
    ];
    // No snapshots provided — liveLtp will be null or use last_price fallback
    const result = computePositionsDayPnl(positions, {}, true);

    // livePositionDayPnl falls through to baseDayPnlForPosition
    // oq=10, dcv=50 → returns 50
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
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 1050, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, false);

    // marketOpen=false → live path gated off → baseDayPnlForPosition returns dcv=50
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
      }),
      makePositionRow({
        tradingsymbol: 'INFY25AUGFUT',
        exchange: 'NFO',
        quantity: 10,
        close_price: 1000,
        last_price: 1005,
        overnight_quantity: 10,
        day_change_val: 50,
      }),
    ];
    const snapshots = {
      RELIANCE: { ltp: 1010, ltp_ts: 1 },
      INFY25AUGFUT: { ltp: 1010, ltp_ts: 1 },
    };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // Pos 1: (1010-1000)*5 = 50
    // Pos 2: (1010-1000)*10 = 100
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
      }),
      makePositionRow({
        tradingsymbol: 'EURINR25AUGFUT',
        exchange: 'CDS',
        quantity: 1,
        close_price: 88,
        last_price: 89,
        overnight_quantity: 1,
        day_change_val: 1,
      }),
      makePositionRow({
        tradingsymbol: 'NIFTY25AUGFUT',
        exchange: 'NFO',
        quantity: 1,
        close_price: 24000,
        last_price: 24100,
        overnight_quantity: 1,
        day_change_val: 100,
      }),
    ];
    const snapshots = {
      CRUDEOIL25AUGFUT: { ltp: 510, ltp_ts: 1 },
      EURINR25AUGFUT: { ltp: 90, ltp_ts: 1 },
      NIFTY25AUGFUT: { ltp: 24200, ltp_ts: 1 },
    };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // MCX: (510-500)*2 = 20
    // CDS: (90-88)*1 = 2
    // NFO: (24200-24000)*1 = 200
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
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 480, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // Live path: realisedToday = 50 - (490-500)*(-5) = 50-50 = 0
    // result = 0 + (480-500)*(-5) = 0 + 100 = 100
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
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 520, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // Live path: realisedToday = -100 - (510-500)*(-10) = -100+100 = 0
    // result = 0 + (520-500)*(-10) = -200
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
      }),
      makePositionRow({
        tradingsymbol: 'NIFTY25AUG100CE',
        quantity: 1,
        close_price: 50,
        last_price: 55,
        overnight_quantity: 1,
        day_change_val: 5,
      }),
    ];
    const snapshots = {
      NIFTY25AUG100PE: { ltp: 60, ltp_ts: 1 },
      NIFTY25AUG100CE: { ltp: 60, ltp_ts: 1 },
    };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // PE short: (60-50)*(-1) = -10
    // CE long: (60-50)*(1) = 10
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
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 0, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // ltp=0 fails > 0 guard → falls back to baseDayPnlForPosition
    // oq=5, dcv=25 → returns 25
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
        pnl: 50, // (110-100)*5
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 110, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // New-position branch: (110-100)*5 = 50
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
        pnl: 15, // (195-200)*(-3)
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 195, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // New-position branch for short: (195-200)*(-3) = 15
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
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 220, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // Live path: realisedToday = -25 - (215-210)*(-15) = -25+75 = 50
    // result = 50 + (220-210)*(-15) = 50-150 = -100
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
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 1020, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // Live path: realisedToday = 75 - (1015-1000)*10 = 75-150 = -75
    // result = -75 + (1020-1000)*10 = -75+200 = 125
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
      }),
      makePositionRow({
        tradingsymbol: 'BANKNIFTY25AUGFUT',
        exchange: 'NFO',
        quantity: 1,
        close_price: 52000,
        last_price: 51900,
        overnight_quantity: 1,
        day_change_val: -100,
      }),
    ];
    const snapshots = {
      NIFTY25AUGFUT: { ltp: 24200, ltp_ts: 1 },
      BANKNIFTY25AUGFUT: { ltp: 51800, ltp_ts: 1 },
    };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // NIFTY: (24200-24000)*1 = 200
    // BANKNIFTY: (51800-52000)*1 = -200
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
      }),
      makePositionRow({
        tradingsymbol: 'P2',
        quantity: 1,
        close_price: 200,
        last_price: 190,
        overnight_quantity: 1,
        day_change_val: -10,
      }),
      makePositionRow({
        tradingsymbol: 'P3',
        quantity: 2,
        close_price: 50,
        last_price: 52,
        overnight_quantity: 2,
        day_change_val: 4,
      }),
    ];
    const snapshots = {
      P1: { ltp: 110, ltp_ts: 1 },
      P2: { ltp: 185, ltp_ts: 1 },
      P3: { ltp: 55, ltp_ts: 1 },
    };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // P1: (110-100)*1 = 10
    // P2: (185-200)*1 = -15
    // P3: (55-50)*2 = 10
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
      }),
    ];
    const snapshots = { INFY25AUGFUT: { ltp: 1235.1, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // (1235.1 - 1234.567) * 2.5 ≈ 1.3325
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
      }),
    ];
    const snapshots = { CRUDEOIL25SEPTFUT: { ltp: 6520, ltp_ts: 1 } };
    const result = computePositionsDayPnl(positions, snapshots, true);

    // (6520-6500)*100 = 2000
    expect(result.byKey['MCX:CRUDEOIL25SEPTFUT']).toBeCloseTo(2000, 4);
  });
});
