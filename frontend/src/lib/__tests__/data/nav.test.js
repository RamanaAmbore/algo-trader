import { describe, it, expect } from 'vitest';
import {
  currentTotalProfit,
  baseDayPnlForPosition,
  aggregateDayPnlForPositions,
  navTotalRow,
  navByAccount,
  positionsPnlFiltered,
  dayChangePct,
} from '$lib/data/nav.js';

// ── currentTotalProfit ───────────────────────────────────────────────────────

describe('currentTotalProfit', () => {
  it('realised + unrealised when both present and finite', () => {
    expect(currentTotalProfit({ realised: 200, unrealised: 300, pnl: 999 })).toBe(500);
  });

  // Mirrors backend's resolve_realised_unrealised (pnl_math.py) EXACTLY:
  // only fall back to `pnl` when BOTH legs are exactly 0/missing — a
  // legitimately-zero SINGLE leg (fresh open position, realised=0,
  // unrealised>0, or vice versa) must NOT trigger the fallback.

  it('does not fall back to pnl when only unrealised is present (realised missing→0)', () => {
    expect(currentTotalProfit({ unrealised: 300, pnl: 150 })).toBe(300);
  });

  it('does not fall back to pnl when only realised is present (unrealised missing→0)', () => {
    expect(currentTotalProfit({ realised: 200, pnl: 150 })).toBe(200);
  });

  it('falls back to pnl when both are absent (cached/closed-hours row)', () => {
    expect(currentTotalProfit({ pnl: 750 })).toBe(750);
  });

  it('falls back to pnl when realised=0 and unrealised=0 (not split / not populated)', () => {
    expect(currentTotalProfit({ realised: 0, unrealised: 0, pnl: 999 })).toBe(999);
  });

  it('no fields at all → 0', () => {
    expect(currentTotalProfit({})).toBe(0);
    expect(currentTotalProfit(null)).toBe(0);
  });
});

// ── baseDayPnlForPosition ────────────────────────────────────────────────────
// Atomic formula: current_total_profit − base_pnl (base_pnl = prev_settlement_pnl,
// defaulting to 0 when absent/non-finite — e.g. a position opened today with no
// prior close-reset snapshot).

describe('baseDayPnlForPosition', () => {
  it('realised+unrealised present, prev_settlement_pnl finite → total − base', () => {
    const p = { realised: 3000, unrealised: 2000, prev_settlement_pnl: 3000 };
    expect(baseDayPnlForPosition(p)).toBe(2000);
  });

  it('pnl fallback, prev_settlement_pnl finite → pnl − base', () => {
    const p = { pnl: 5000, prev_settlement_pnl: 3000 };
    expect(baseDayPnlForPosition(p)).toBe(2000);
  });

  it('prev_settlement_pnl = 0 (falsy but finite) → total − 0', () => {
    const p = { pnl: 1500, prev_settlement_pnl: 0 };
    expect(baseDayPnlForPosition(p)).toBe(1500);
  });

  it('prev_settlement_pnl null → base defaults to 0 (new position, no prior snapshot)', () => {
    const p = { pnl: 1000, prev_settlement_pnl: null };
    expect(baseDayPnlForPosition(p)).toBe(1000);
  });

  it('prev_settlement_pnl absent → base defaults to 0', () => {
    const p = { pnl: 2000 };
    expect(baseDayPnlForPosition(p)).toBe(2000);
  });

  it('all zeros → returns 0', () => {
    const p = { pnl: 0, prev_settlement_pnl: 0 };
    expect(baseDayPnlForPosition(p)).toBe(0);
  });

  it('NaN prev_settlement_pnl → base defaults to 0 (not finite)', () => {
    const p = { pnl: 1000, prev_settlement_pnl: NaN };
    expect(baseDayPnlForPosition(p)).toBe(1000);
  });

  it('short position (negative qty is irrelevant to the formula — pnl already signed)', () => {
    const p = { pnl: -2000, prev_settlement_pnl: -500 };
    expect(baseDayPnlForPosition(p)).toBe(-1500);
  });

  it('closed-today position (qty=0): realised carries the full day move', () => {
    const p = { realised: 750, unrealised: 0, prev_settlement_pnl: 0 };
    expect(baseDayPnlForPosition(p)).toBe(750);
  });

  it('holdings-sold-into-positions: base_pnl from prior kind carries through prev_settlement_pnl', () => {
    // Backend resolves prev_settlement_pnl across kind='positions'/'holdings' —
    // frontend just consumes whatever baseline the backend supplies.
    const p = { pnl: 9000, prev_settlement_pnl: 8500 };
    expect(baseDayPnlForPosition(p)).toBe(500);
  });
});

// ── aggregateDayPnlForPositions ──────────────────────────────────────────────

describe('aggregateDayPnlForPositions', () => {
  it('sums baseDayPnlForPosition across rows', () => {
    const rows = [
      { pnl: 5000, prev_settlement_pnl: 3000 },  // → 2000
      { pnl: 1000, prev_settlement_pnl: null },  // new pos, no baseline → 1000
    ];
    expect(aggregateDayPnlForPositions(rows)).toBe(3000);
  });

  it('empty array → 0', () => {
    expect(aggregateDayPnlForPositions([])).toBe(0);
  });

  it('handles single row', () => {
    const rows = [{ pnl: 800, prev_settlement_pnl: 300 }];
    expect(aggregateDayPnlForPositions(rows)).toBe(500);
  });
});

// ── livePositionDayPnl removal regression guard ─────────────────────────────
//
// §1 (positions/holdings LTP-source redesign): the live-tick delta wrapper
// `livePositionDayPnl` was removed from nav.js — positions/holdings Day P&L
// is now purely poll-driven via `baseDayPnlForPosition` alone, with NO
// `(liveLtp − pollLtp) × qty` adjustment layered on top. These tests guard
// against the delta term being reintroduced: baseDayPnlForPosition's result
// must be identical regardless of any "live LTP" context a caller might have
// (there is no live-LTP parameter left to pass — the function signature
// itself is the guard).
describe('baseDayPnlForPosition — no live-tick sensitivity (§1 regression guard)', () => {
  it('base = pnl − prev_settlement_pnl, independent of any live-price context', () => {
    const dcvRow = { pnl: 110, prev_settlement_pnl: 100 };
    expect(baseDayPnlForPosition(dcvRow)).toBe(10);
  });

  it('short position: base is signed pnl-diff only, no qty-scaled tick delta', () => {
    const dcvRow = { pnl: -10, prev_settlement_pnl: -20 };
    expect(baseDayPnlForPosition(dcvRow)).toBe(10);
  });

  it('new position (no prev_settlement_pnl): base = pnl, no tick adjustment possible', () => {
    const dcvRow = { pnl: 0, prev_settlement_pnl: null };
    expect(baseDayPnlForPosition(dcvRow)).toBe(0);
  });

  it('flat settlement (pnl = prev_settlement_pnl): base is honestly 0', () => {
    const dcvRow = { pnl: -5000, prev_settlement_pnl: -5000 };
    expect(baseDayPnlForPosition(dcvRow)).toBe(0);
  });

  it('function signature takes only the row — no liveLtp/pollLtp/marketOpen params', () => {
    expect(baseDayPnlForPosition.length).toBe(1);
  });
});

// ── navTotalRow ──────────────────────────────────────────────────────────────

describe('navTotalRow', () => {
  it('sums nav fields across rows', () => {
    const rows = [
      { account: 'AA1111', cash: 50000, pos_m2m: 10000, holdings_mtm: 20000, nav: 80000 },
      { account: 'BB2222', cash: 30000, pos_m2m:  5000, holdings_mtm: 15000, nav: 50000 },
    ];
    const total = navTotalRow(rows);
    expect(total.account).toBe('TOTAL');
    expect(total.cash).toBe(80000);
    expect(total.pos_m2m).toBe(15000);
    expect(total.holdings_mtm).toBe(35000);
    expect(total.nav).toBe(130000);
  });

  it('empty array → null', () => {
    expect(navTotalRow([])).toBeNull();
  });

  it('null → null', () => {
    expect(navTotalRow(null)).toBeNull();
  });

  it('single row → total equals that row', () => {
    const rows = [{ account: 'X', cash: 1000, pos_m2m: 200, holdings_mtm: 300, nav: 1500 }];
    const total = navTotalRow(rows);
    expect(total.nav).toBe(1500);
  });
});

// ── navByAccount ─────────────────────────────────────────────────────────────

describe('navByAccount', () => {
  it('computes nav for each account from funds/positions/holdings', () => {
    const funds = [{ account: 'AA', cash: 100000, option_premium: 5000 }];
    const positions = [{ account: 'AA', unrealised: 8000 }];
    const holdings = [{ account: 'AA', cur_val: 20000 }];
    const rows = navByAccount(['AA'], funds, positions, holdings);
    expect(rows).toHaveLength(1);
    const r = rows[0];
    expect(r.account).toBe('AA');
    expect(r.cash).toBe(105000);        // 100000 + 5000
    expect(r.pos_m2m).toBe(8000);
    expect(r.holdings_mtm).toBe(20000);
    expect(r.nav).toBe(133000);
  });

  it('empty accounts array → empty rows', () => {
    expect(navByAccount([], [], [], [])).toEqual([]);
  });

  it('missing funds row → cash = 0', () => {
    const rows = navByAccount(['ZZ'], [], [], []);
    expect(rows[0].cash).toBe(0);
    expect(rows[0].nav).toBe(0);
  });
});

// ── positionsPnlFiltered ─────────────────────────────────────────────────────

describe('positionsPnlFiltered', () => {
  it('is exported as a function', () => {
    expect(typeof positionsPnlFiltered).toBe('function');
  });

  it('sums pnl + dayTotal for F&O exchanges only (NFO, BFO, MCX, CDS)', () => {
    const positions = [
      { exchange: 'NFO', pnl: 3000, prev_settlement_pnl: 1000 }, // dayTotal = 3000 - 1000 = 2000
      { exchange: 'MCX', pnl: 1500, prev_settlement_pnl: 500 },  // dayTotal = 1500 - 500 = 1000
      { exchange: 'NSE', pnl: 999,  prev_settlement_pnl: 0 },    // excluded (equity)
    ];
    const { pnlTotal, dayTotal } = positionsPnlFiltered(positions);
    expect(pnlTotal).toBe(4500);  // 3000 + 1500
    expect(dayTotal).toBe(3000);  // 2000 + 1000
  });

  it('excludes NSE/BSE (equity) exchanges entirely', () => {
    const positions = [
      { exchange: 'NSE', pnl: 10000, prev_settlement_pnl: 0 },
      { exchange: 'BSE', pnl: 5000,  prev_settlement_pnl: 0 },
    ];
    const { pnlTotal, dayTotal } = positionsPnlFiltered(positions);
    expect(pnlTotal).toBe(0);
    expect(dayTotal).toBe(0);
  });

  it('null / undefined positions → zeros', () => {
    expect(positionsPnlFiltered(null)).toEqual({ pnlTotal: 0, dayTotal: 0 });
    expect(positionsPnlFiltered(undefined)).toEqual({ pnlTotal: 0, dayTotal: 0 });
  });

  it('empty array → zeros', () => {
    expect(positionsPnlFiltered([])).toEqual({ pnlTotal: 0, dayTotal: 0 });
  });
});

// ── dayChangePct ─────────────────────────────────────────────────────────────

describe('dayChangePct', () => {
  it('returns (dayPnl / prevMv) * 100 for valid inputs', () => {
    expect(dayChangePct(500, 10000)).toBeCloseTo(5);
  });

  it('returns null when prevMv is 0', () => {
    expect(dayChangePct(500, 0)).toBeNull();
  });

  it('returns null when prevMv is negative', () => {
    expect(dayChangePct(500, -1000)).toBeNull();
  });

  it('returns null when dayPnl is not finite (NaN)', () => {
    expect(dayChangePct(NaN, 10000)).toBeNull();
  });

  it('returns null when dayPnl is not finite (Infinity)', () => {
    expect(dayChangePct(Infinity, 10000)).toBeNull();
  });

  it('handles negative dayPnl (loss)', () => {
    expect(dayChangePct(-300, 10000)).toBeCloseTo(-3);
  });

  it('zero dayPnl returns 0% not null', () => {
    expect(dayChangePct(0, 5000)).toBeCloseTo(0);
  });
});
