import { describe, it, expect } from 'vitest';
import {
  baseDayPnlForPosition,
  aggregateDayPnlForPositions,
  livePositionDayPnl,
  navTotalRow,
  navByAccount,
} from '$lib/data/nav.js';

// ── baseDayPnlForPosition ────────────────────────────────────────────────────

describe('baseDayPnlForPosition', () => {
  it('authoritative path: prev_settlement_pnl finite → pnl - prev_settlement_pnl', () => {
    const p = { pnl: 5000, prev_settlement_pnl: 3000, overnight_quantity: 2, day_change_val: 0, close_price: 50, average_price: 45 };
    expect(baseDayPnlForPosition(p)).toBe(2000);
  });

  it('authoritative path: prev_settlement_pnl = 0 (falsy but finite)', () => {
    const p = { pnl: 1500, prev_settlement_pnl: 0 };
    expect(baseDayPnlForPosition(p)).toBe(1500);
  });

  it('fallback: prev_settlement_pnl null, oq > 0, dcv !== 0 → returns dcv', () => {
    const p = { pnl: 1000, prev_settlement_pnl: null, overnight_quantity: 5, day_change_val: 800 };
    expect(baseDayPnlForPosition(p)).toBe(800);
  });

  it('fallback: prev_settlement_pnl absent, oq > 0, dcv !== 0 → returns dcv', () => {
    const p = { pnl: 2000, overnight_quantity: 3, day_change_val: 1200, close_price: 100, average_price: 90 };
    expect(baseDayPnlForPosition(p)).toBe(1200);
  });

  it('Case 3: oq > 0, dcv === 0, close > 0 → pnl - oq*(close - avg)', () => {
    // e.g. oq=2, close=100, avg=80 → overnight_carry = 2*(100-80)=40; day P&L = 200 - 40 = 160
    const p = { pnl: 200, overnight_quantity: 2, day_change_val: 0, close_price: 100, average_price: 80 };
    expect(baseDayPnlForPosition(p)).toBe(160);
  });

  it('Case 4: oq > 0, dcv === 0, close <= 0 → returns 0 (MCX zero-close guard)', () => {
    const p = { pnl: 500, overnight_quantity: 2, day_change_val: 0, close_price: 0, average_price: 80 };
    expect(baseDayPnlForPosition(p)).toBe(0);
  });

  it('Case 4: close negative → returns 0', () => {
    const p = { pnl: 500, overnight_quantity: 2, day_change_val: 0, close_price: -1, average_price: 80 };
    expect(baseDayPnlForPosition(p)).toBe(0);
  });

  it('new intraday position: oq === 0, pnl !== 0 → returns pnl', () => {
    // oq=0, dcv=0, close=0 → pnl - 0*(0-avg) = pnl
    const p = { pnl: 750, overnight_quantity: 0, day_change_val: 0, close_price: 0, average_price: 100 };
    expect(baseDayPnlForPosition(p)).toBe(750);
  });

  it('all zeros → returns 0', () => {
    const p = { pnl: 0, overnight_quantity: 0, day_change_val: 0, close_price: 0, average_price: 0 };
    expect(baseDayPnlForPosition(p)).toBe(0);
  });

  it('uses prev_close fallback when close_price absent', () => {
    // oq > 0, dcv = 0, prev_close > 0
    const p = { pnl: 300, overnight_quantity: 1, day_change_val: 0, prev_close: 200, average_price: 190 };
    expect(baseDayPnlForPosition(p)).toBe(300 - 1 * (200 - 190));  // 290
  });

  it('uses avg_cost fallback when average_price absent', () => {
    const p = { pnl: 300, overnight_quantity: 1, day_change_val: 0, close_price: 200, avg_cost: 190 };
    expect(baseDayPnlForPosition(p)).toBe(300 - 1 * (200 - 190));  // 290
  });

  it('NaN prev_settlement_pnl falls through to fallback path', () => {
    // NaN is not finite, so fallback applies; oq > 0, dcv = 500
    const p = { pnl: 1000, prev_settlement_pnl: NaN, overnight_quantity: 2, day_change_val: 500 };
    expect(baseDayPnlForPosition(p)).toBe(500);
  });
});

// ── aggregateDayPnlForPositions ──────────────────────────────────────────────

describe('aggregateDayPnlForPositions', () => {
  it('sums baseDayPnlForPosition across rows', () => {
    const rows = [
      { pnl: 5000, prev_settlement_pnl: 3000 },  // → 2000
      { pnl: 1000, overnight_quantity: 0, day_change_val: 0, close_price: 0 }, // new pos → 1000
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

// ── livePositionDayPnl ───────────────────────────────────────────────────────

describe('livePositionDayPnl', () => {
  const makeFields = (overrides = {}) => ({
    closePx: 100,
    pollLtp: 102,
    qty: 5,
    avg: 98,
    dcvRow: { pnl: 10, overnight_quantity: 5, day_change_val: 10, close_price: 100 },
    ...overrides,
  });

  it('market closed → falls back to baseDayPnlForPosition', () => {
    const fields = makeFields();
    // baseDayPnlForPosition of dcvRow: oq>0, dcv=10 (non-zero) → 10
    const result = livePositionDayPnl(fields, 105, { marketOpen: false });
    expect(result).toBe(10);
  });

  it('market open + liveLtp > 0 + closePx > 0 → live recompute', () => {
    // brokerDcv = 10 (dcv=10, oq=5, dcv!=0 → 10)
    // realisedToday = 10 - (102 - 100)*5 = 10 - 10 = 0
    // result = 0 + (105 - 100)*5 = 25
    const fields = makeFields();
    const result = livePositionDayPnl(fields, 105, { marketOpen: true });
    expect(result).toBe(25);
  });

  it('new position: closePx=0, avg>0 → (liveLtp - avg) * qty', () => {
    const fields = makeFields({ closePx: 0, pollLtp: 0, qty: 3, avg: 50,
      dcvRow: { pnl: 0, overnight_quantity: 0, day_change_val: 0, close_price: 0 } });
    // closePx=0, avg>0, qty≠0 → (60 - 50)*3 = 30
    const result = livePositionDayPnl(fields, 60, { marketOpen: true });
    expect(result).toBe(30);
  });

  it('liveLtp absent (null) → falls back to base', () => {
    const fields = makeFields();
    // dcvRow: dcv=10, oq=5, dcv!=0 → 10
    const result = livePositionDayPnl(fields, null, { marketOpen: true });
    expect(result).toBe(10);
  });

  it('liveLtp = 0 (zero, not positive) → falls back to base', () => {
    const fields = makeFields();
    const result = livePositionDayPnl(fields, 0, { marketOpen: true });
    expect(result).toBe(10);
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
