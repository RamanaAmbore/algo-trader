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
  it('market open + short qty (-5): live delta applied with correct sign', () => {
    // base = pnl(-10), no prev_settlement_pnl → base = -10
    // delta = (105 - 102) * (-5) = -15 → result = -25
    const fields = { pollLtp: 102, qty: -5, dcvRow: { pnl: -10, prev_settlement_pnl: null } };
    expect(livePositionDayPnl(fields, 105, { marketOpen: true })).toBe(-25);
  });

  it('market open + short qty (-3): live price below poll → gain for short', () => {
    // base = pnl(30) - prev_settlement_pnl(15) = 15
    // delta = (195 - 198) * (-3) = 9 → result = 24
    const fields = { pollLtp: 198, qty: -3, dcvRow: { pnl: 30, prev_settlement_pnl: 15 } };
    expect(livePositionDayPnl(fields, 195, { marketOpen: true })).toBe(24);
  });

  it('market open + short qty (-10): liveLtp > pollLtp (price spiked — short loses)', () => {
    // base = pnl(-1000) - prev_settlement_pnl(-900) = -100
    // delta = (520 - 510) * (-10) = -100 → result = -200
    const fields = { pollLtp: 510, qty: -10, dcvRow: { pnl: -1000, prev_settlement_pnl: -900 } };
    expect(livePositionDayPnl(fields, 520, { marketOpen: true })).toBe(-200);
  });
});

// ── livePositionDayPnl — live delta applied on top of the settlement-baseline
// base (formerly a "naive vs decomposed" distinction under the old Case
// machinery — the new atomic formula makes both paths identical by
// construction since the live delta always applies to the FULL net qty).

describe('livePositionDayPnl — live delta on top of settlement-diff base', () => {
  it('base already reflects intraday adds (via realised/unrealised); live delta layers on top', () => {
    // base = pnl(-125) - prev_settlement_pnl(-100) = -25
    // delta = (220 - 215) * (-15) = -75 → result = -100
    const fields = { pollLtp: 215, qty: -15, dcvRow: { pnl: -125, prev_settlement_pnl: -100 } };
    expect(livePositionDayPnl(fields, 220, { marketOpen: true })).toBe(-100);
  });

  it('pure overnight (no intraday adds): base + live delta', () => {
    // base = pnl(-150) - prev_settlement_pnl(-100) = -50
    // delta = (220 - 215) * (-10) = -50 → result = -100
    const fields = { pollLtp: 215, qty: -10, dcvRow: { pnl: -150, prev_settlement_pnl: -100 } };
    expect(livePositionDayPnl(fields, 220, { marketOpen: true })).toBe(-100);
  });
});

// ── livePositionDayPnl — pollLtp = 0 (broker hasn't populated last_price) ───

describe('livePositionDayPnl — pollLtp=0 edge', () => {
  it('pollLtp=0: live delta not applied (would blow up to liveLtp × qty), returns base', () => {
    const fields = { pollLtp: 0, qty: 5, dcvRow: { pnl: 200, prev_settlement_pnl: 150 } };
    expect(livePositionDayPnl(fields, 105, { marketOpen: true })).toBe(50);
  });

  it('pollLtp=0 with short qty: returns base only, no live delta', () => {
    const fields = { pollLtp: 0, qty: -4, dcvRow: { pnl: -400, prev_settlement_pnl: -360 } };
    expect(livePositionDayPnl(fields, 210, { marketOpen: true })).toBe(-40);
  });
});

// ── livePositionDayPnl — new position (no prev_settlement_pnl) ──────────────

describe('livePositionDayPnl — new position edge cases', () => {
  it('no prev_settlement_pnl, qty≠0, market open → base(=pnl) + live delta', () => {
    const fields = { pollLtp: 50, qty: 2, dcvRow: { pnl: 0, prev_settlement_pnl: null } };
    // base=0, delta=(60-50)*2=20 → result=20
    expect(livePositionDayPnl(fields, 60, { marketOpen: true })).toBe(20);
  });

  it('qty=0 → live delta skipped, returns base', () => {
    const fields = { pollLtp: 0, qty: 0, dcvRow: { pnl: 0, prev_settlement_pnl: null } };
    expect(livePositionDayPnl(fields, 60, { marketOpen: true })).toBe(0);
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

  it('overnight position, negative baseline delta (short moving against)', () => {
    // pnl=-1000, prev_settlement_pnl=-700 → day pnl = -300
    const p = { pnl: -1000, prev_settlement_pnl: -700 };
    expect(baseDayPnlForPosition(p)).toBe(-300);
  });

  it('null position object → returns 0', () => {
    expect(baseDayPnlForPosition(null)).toBe(0);
  });

  it('undefined position object → returns 0', () => {
    expect(baseDayPnlForPosition(undefined)).toBe(0);
  });
});
