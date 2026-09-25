/**
 * positions.test.js — scenarios for baseDayPnlForPosition and
 * aggregateDayPnlForPositions that are NOT covered by nav.test.js.
 *
 * nav.test.js already covers:
 *   - baseDayPnlForPosition (all paths)
 *   - aggregateDayPnlForPositions (empty, single, multi-row sum)
 *
 * This file adds genuinely new scenarios: short positions (negative qty),
 * pollLtp=0 edge in realisedToday, avg=0 guard, multi-row with negative pnl.
 *
 * §1 (positions/holdings LTP-source redesign): `livePositionDayPnl` (the
 * live-tick delta wrapper) was removed from nav.js. The describe blocks
 * below that used to exercise its `(liveLtp − pollLtp) × qty` delta now
 * assert baseDayPnlForPosition alone, as a regression guard that no tick
 * delta is reintroduced — the `pollLtp`/`qty` fields in each row are still
 * present in the fixtures (mirroring real broker rows) but are provably
 * irrelevant to the result, since baseDayPnlForPosition never reads them.
 */

import { describe, it, expect } from 'vitest';
import {
  aggregateDayPnlForPositions,
  baseDayPnlForPosition,
} from '$lib/data/nav.js';

// ── baseDayPnlForPosition — short position (negative qty) ───────────────────

describe('baseDayPnlForPosition — short position (no live-tick delta)', () => {
  it('short qty (-5), no prev_settlement_pnl: base = pnl only', () => {
    const dcvRow = { pnl: -10, prev_settlement_pnl: null };
    expect(baseDayPnlForPosition(dcvRow)).toBe(-10);
  });

  it('short qty (-3): base = pnl − prev_settlement_pnl, qty sign is irrelevant', () => {
    const dcvRow = { pnl: 30, prev_settlement_pnl: 15 };
    expect(baseDayPnlForPosition(dcvRow)).toBe(15);
  });

  it('short qty (-10), large numbers: base = pnl − prev_settlement_pnl', () => {
    const dcvRow = { pnl: -1000, prev_settlement_pnl: -900 };
    expect(baseDayPnlForPosition(dcvRow)).toBe(-100);
  });
});

// ── baseDayPnlForPosition — settlement-diff base, no tick layering ──────────

describe('baseDayPnlForPosition — settlement-diff base only', () => {
  it('base already reflects intraday adds (via realised/unrealised); no tick delta layers on top', () => {
    const dcvRow = { pnl: -125, prev_settlement_pnl: -100 };
    expect(baseDayPnlForPosition(dcvRow)).toBe(-25);
  });

  it('pure overnight (no intraday adds): base is the full result, unchanged by any live price', () => {
    const dcvRow = { pnl: -150, prev_settlement_pnl: -100 };
    expect(baseDayPnlForPosition(dcvRow)).toBe(-50);
  });
});

// ── baseDayPnlForPosition — pollLtp=0 in the row is a no-op (field unread) ──

describe('baseDayPnlForPosition — pollLtp/qty fields present but unread', () => {
  it('pollLtp=0 in the row does not affect the result', () => {
    const dcvRow = { pnl: 200, prev_settlement_pnl: 150, last_price: 0, quantity: 5 };
    expect(baseDayPnlForPosition(dcvRow)).toBe(50);
  });

  it('pollLtp=0 with short qty: result is base only', () => {
    const dcvRow = { pnl: -400, prev_settlement_pnl: -360, last_price: 0, quantity: -4 };
    expect(baseDayPnlForPosition(dcvRow)).toBe(-40);
  });
});

// ── baseDayPnlForPosition — new position (no prev_settlement_pnl) ───────────

describe('baseDayPnlForPosition — new position edge cases', () => {
  it('no prev_settlement_pnl, qty≠0 → base = pnl, no tick delta possible', () => {
    const dcvRow = { pnl: 0, prev_settlement_pnl: null, quantity: 2 };
    expect(baseDayPnlForPosition(dcvRow)).toBe(0);
  });

  it('qty=0 → base unaffected either way', () => {
    const dcvRow = { pnl: 0, prev_settlement_pnl: null, quantity: 0 };
    expect(baseDayPnlForPosition(dcvRow)).toBe(0);
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
