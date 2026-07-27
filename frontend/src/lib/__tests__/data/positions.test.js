/**
 * positions.test.js — new scenarios for livePositionDayPnl and
 * aggregateDayPnlForPositions that are NOT covered by nav.test.js.
 *
 * nav.test.js already covers:
 *   - baseDayPnlForPosition (all paths)
 *   - aggregateDayPnlForPositions (empty, single, multi-row sum)
 *   - livePositionDayPnl (market closed, live recompute, new position, null/zero liveLtp)
 *
 * This file adds genuinely new scenarios: short positions (negative qty),
 * pollLtp=0 edge in realisedToday, avg=0 guard, multi-row with negative pnl.
 */

import { describe, it, expect } from 'vitest';
import {
  livePositionDayPnl,
  aggregateDayPnlForPositions,
  baseDayPnlForPosition,
} from '$lib/data/nav.js';

// ── livePositionDayPnl — short position (negative qty) ──────────────────────

describe('livePositionDayPnl — short position', () => {
  it('market open + short qty (-5): live recompute flips sign', () => {
    // closePx=100, pollLtp=102, qty=-5
    // dcvRow: oq=5(abs overnight), dcv=-10 (negative for short)
    //   baseDayPnlForPosition → dcv=-10 (oq>0, dcv≠0)
    // brokerDcv = -10
    // realisedToday = -10 - (102 - 100)*(-5) = -10 + 10 = 0
    // result = 0 + (105 - 100)*(-5) = -25
    const fields = {
      closePx: 100,
      pollLtp: 102,
      qty: -5,
      avg: 98,
      dcvRow: { pnl: -10, overnight_quantity: 5, day_change_val: -10, close_price: 100 },
    };
    expect(livePositionDayPnl(fields, 105, { marketOpen: true })).toBe(-25);
  });

  it('market open + short qty (-3): live price below close → gain for short', () => {
    // closePx=200, pollLtp=198, qty=-3
    // dcvRow: oq=3, dcv=6 (3 * (200-198) = price fell, short gains)
    //   baseDayPnlForPosition → dcv=6
    // brokerDcv = 6
    // realisedToday = 6 - (198 - 200)*(-3) = 6 - 6 = 0
    // result = 0 + (195 - 200)*(-3) = 0 + 15 = 15
    const fields = {
      closePx: 200,
      pollLtp: 198,
      qty: -3,
      avg: 205,
      dcvRow: { pnl: 30, overnight_quantity: 3, day_change_val: 6, close_price: 200 },
    };
    expect(livePositionDayPnl(fields, 195, { marketOpen: true })).toBe(15);
  });

  it('market open + short qty (-10): liveLtp > pollLtp (price spiked — short loses)', () => {
    // closePx=500, pollLtp=510, qty=-10
    // dcvRow: oq=10, dcv=-100 (short loses when price rises)
    //   baseDayPnlForPosition → dcv=-100
    // brokerDcv = -100
    // realisedToday = -100 - (510 - 500)*(-10) = -100 + 100 = 0
    // result = 0 + (520 - 500)*(-10) = -200
    const fields = {
      closePx: 500,
      pollLtp: 510,
      qty: -10,
      avg: 495,
      dcvRow: { pnl: -1000, overnight_quantity: 10, day_change_val: -100, close_price: 500 },
    };
    expect(livePositionDayPnl(fields, 520, { marketOpen: true })).toBe(-200);
  });
});

// ── livePositionDayPnl — pollLtp = 0 (broker hasn't populated last_price) ───

describe('livePositionDayPnl — pollLtp=0 edge', () => {
  it('pollLtp=0: realisedToday falls back to 0, result uses live-close delta only', () => {
    // pollLtp=0, closePx=100 → pollLtp > 0 is false → realisedToday = 0
    // result = 0 + (105 - 100)*5 = 25
    const fields = {
      closePx: 100,
      pollLtp: 0,
      qty: 5,
      avg: 90,
      dcvRow: { pnl: 200, overnight_quantity: 5, day_change_val: 50, close_price: 100 },
    };
    expect(livePositionDayPnl(fields, 105, { marketOpen: true })).toBe(25);
  });

  it('pollLtp=0 with short qty: result = (live - close) * qty = negative', () => {
    // closePx=200, pollLtp=0, qty=-4, liveLtp=210
    // realisedToday = 0 (pollLtp not > 0)
    // result = 0 + (210 - 200)*(-4) = -40
    const fields = {
      closePx: 200,
      pollLtp: 0,
      qty: -4,
      avg: 205,
      dcvRow: { pnl: -400, overnight_quantity: 4, day_change_val: -40, close_price: 200 },
    };
    expect(livePositionDayPnl(fields, 210, { marketOpen: true })).toBe(-40);
  });
});

// ── livePositionDayPnl — new position, closePx=0, avg=0 ─────────────────────

describe('livePositionDayPnl — new position edge cases', () => {
  it('closePx=0, avg=0, qty≠0 → does NOT enter new-position branch (avg=0 fails guard)', () => {
    // The new-position branch requires avg > 0.
    // Falls through to baseDayPnlForPosition(dcvRow).
    // dcvRow: oq=0, pnl=500 → returns pnl=500
    const fields = {
      closePx: 0,
      pollLtp: 0,
      qty: 2,
      avg: 0,
      dcvRow: { pnl: 500, overnight_quantity: 0, day_change_val: 0, close_price: 0 },
    };
    const result = livePositionDayPnl(fields, 60, { marketOpen: true });
    // closePx=0: first branch fails (closePx must be > 0)
    // Second branch: closePx===0 but avg=0, so avg>0 fails → falls to base
    // baseDayPnlForPosition: oq=0, dcv=0, close=0 → pnl - 0*(0-0) = 500
    expect(result).toBe(500);
  });

  it('closePx=0, avg>0, qty=0 → new-position branch skipped (qty=0)', () => {
    // qty=0 → new-position branch requires qty≠0
    const fields = {
      closePx: 0,
      pollLtp: 0,
      qty: 0,
      avg: 50,
      dcvRow: { pnl: 0, overnight_quantity: 0, day_change_val: 0, close_price: 0 },
    };
    const result = livePositionDayPnl(fields, 60, { marketOpen: true });
    expect(result).toBe(0);
  });
});

// ── aggregateDayPnlForPositions — negative pnl / short in loss ──────────────

describe('aggregateDayPnlForPositions — negative pnl scenarios', () => {
  it('single short-in-loss position: pnl negative, prev_settlement_pnl present', () => {
    // pnl=-2000, prev_settlement_pnl=-500 → day pnl = -2000 - (-500) = -1500
    const rows = [{ pnl: -2000, prev_settlement_pnl: -500 }];
    expect(aggregateDayPnlForPositions(rows)).toBe(-1500);
  });

  it('mixed profit + loss positions aggregate correctly', () => {
    // Row A: pnl=3000, prev=1000 → day=2000
    // Row B: pnl=-1500, prev=0   → day=-1500
    // Total: 500
    const rows = [
      { pnl: 3000, prev_settlement_pnl: 1000 },
      { pnl: -1500, prev_settlement_pnl: 0 },
    ];
    expect(aggregateDayPnlForPositions(rows)).toBe(500);
  });

  it('all negative positions: sum is negative', () => {
    const rows = [
      { pnl: -1000, prev_settlement_pnl: 0 },
      { pnl: -2000, prev_settlement_pnl: -500 },
    ];
    // -1000 + (-1500) = -2500
    expect(aggregateDayPnlForPositions(rows)).toBe(-2500);
  });

  it('intraday short closed at loss (oq=0, pnl negative)', () => {
    // oq=0, pnl=-800 → baseDayPnlForPosition returns pnl=-800
    const rows = [{ pnl: -800, overnight_quantity: 0, day_change_val: 0, close_price: 0 }];
    expect(aggregateDayPnlForPositions(rows)).toBe(-800);
  });

  it('empty array still returns 0', () => {
    expect(aggregateDayPnlForPositions([])).toBe(0);
  });
});

// ── aggregateDayPnlForPositions — MCX lot_size effect ───────────────────────

describe('aggregateDayPnlForPositions — MCX lot_size pnl scale', () => {
  it('overnight MCX position: pnl already scaled by lot_size in broker row', () => {
    // Broker ships pnl already in rupee terms (qty * lot_size embedded).
    // prev_settlement_pnl authoritative path strips overnight carry correctly.
    // lot_size=100, 1 lot = 100 contracts, pnl=50000, prev=30000 → day=20000
    const rows = [{ pnl: 50000, prev_settlement_pnl: 30000 }];
    expect(aggregateDayPnlForPositions(rows)).toBe(20000);
  });

  it('multi-lot MCX: two contracts produce proportionate aggregate', () => {
    // Two separate MCX rows (e.g. CRUDEOIL and NATURALGAS)
    const rows = [
      { pnl: 80000, prev_settlement_pnl: 50000 },   // → 30000
      { pnl: -20000, prev_settlement_pnl: -5000 },  // → -15000
    ];
    expect(aggregateDayPnlForPositions(rows)).toBe(15000);
  });
});

// ── baseDayPnlForPosition — negative overnight pnl ──────────────────────────

describe('baseDayPnlForPosition — additional negative-pnl paths', () => {
  it('overnight position with negative pnl and positive prev_settlement', () => {
    // prev_settlement_pnl present → pnl - prev
    // pnl=-500, prev=200 → day pnl = -700
    const p = { pnl: -500, prev_settlement_pnl: 200 };
    expect(baseDayPnlForPosition(p)).toBe(-700);
  });

  it('overnight DCv path: dcv is negative (short moving against)', () => {
    // oq>0, dcv=-300 (non-zero) → returns dcv
    const p = { pnl: -1000, overnight_quantity: 5, day_change_val: -300, close_price: 100, average_price: 90 };
    expect(baseDayPnlForPosition(p)).toBe(-300);
  });

  it('null position object → returns 0', () => {
    expect(baseDayPnlForPosition(null)).toBe(0);
  });

  it('undefined position object → returns 0', () => {
    expect(baseDayPnlForPosition(undefined)).toBe(0);
  });
});
