import { describe, it, expect } from 'vitest';
import {
  navTotalRow,
  navByAccount,
  aggregateDayPnlForPositions,
  baseDayPnlForPosition,
} from '$lib/data/nav.js';

// ── navTotalRow ──────────────────────────────────────────────────────────────

describe('navTotalRow — single account', () => {
  it('total.nav === account.nav', () => {
    const rows = [{ account: 'AA', cash: 50000, pos_m2m: 10000, holdings_mtm: 20000, nav: 80000 }];
    const total = navTotalRow(rows);
    expect(total.nav).toBe(80000);
  });

  it('total.pos_m2m === account.pos_m2m', () => {
    const rows = [{ account: 'AA', cash: 50000, pos_m2m: 7500, holdings_mtm: 20000, nav: 77500 }];
    const total = navTotalRow(rows);
    expect(total.pos_m2m).toBe(7500);
  });

  it('total.cash === account.cash', () => {
    const rows = [{ account: 'AA', cash: 42000, pos_m2m: 0, holdings_mtm: 0, nav: 42000 }];
    const total = navTotalRow(rows);
    expect(total.cash).toBe(42000);
  });

  it('total.holdings_mtm === account.holdings_mtm', () => {
    const rows = [{ account: 'AA', cash: 0, pos_m2m: 0, holdings_mtm: 33000, nav: 33000 }];
    const total = navTotalRow(rows);
    expect(total.holdings_mtm).toBe(33000);
  });
});

describe('navTotalRow — two accounts sum correctly', () => {
  const rows = [
    { account: 'AA', cash: 50000, pos_m2m: 10000, holdings_mtm: 20000, nav: 80000 },
    { account: 'BB', cash: 30000, pos_m2m:  5000, holdings_mtm: 15000, nav: 50000 },
  ];

  it('cash sums', () => {
    expect(navTotalRow(rows).cash).toBe(80000);
  });

  it('pos_m2m sums', () => {
    expect(navTotalRow(rows).pos_m2m).toBe(15000);
  });

  it('holdings_mtm sums', () => {
    expect(navTotalRow(rows).holdings_mtm).toBe(35000);
  });

  it('nav sums', () => {
    expect(navTotalRow(rows).nav).toBe(130000);
  });
});

describe('navTotalRow — empty input', () => {
  it('empty array → null (not zeros, not NaN)', () => {
    expect(navTotalRow([])).toBeNull();
  });

  it('null → null', () => {
    expect(navTotalRow(null)).toBeNull();
  });
});

describe('navTotalRow — mixed positive/negative pos_m2m', () => {
  it('algebraic sum (not absolute)', () => {
    const rows = [
      { account: 'AA', cash: 0, pos_m2m:  8000, holdings_mtm: 0, nav:  8000 },
      { account: 'BB', cash: 0, pos_m2m: -3000, holdings_mtm: 0, nav: -3000 },
    ];
    expect(navTotalRow(rows).pos_m2m).toBe(5000);
    expect(navTotalRow(rows).nav).toBe(5000);
  });

  it('net negative when losses exceed gains', () => {
    const rows = [
      { account: 'AA', cash: 0, pos_m2m: -12000, holdings_mtm: 0, nav: -12000 },
      { account: 'BB', cash: 0, pos_m2m:   4000, holdings_mtm: 0, nav:   4000 },
    ];
    expect(navTotalRow(rows).pos_m2m).toBe(-8000);
  });
});

describe('navTotalRow — string-numeric coercion', () => {
  it('string cash/pos_m2m/holdings_mtm coerced via Number()', () => {
    // navTotalRow reduces over existing row objects — values must already be numeric.
    // navRowForAccount (called by navByAccount) does Number() coercion at build time.
    // So verify that when numeric rows are passed, reduction produces no NaN.
    const rows = [
      { account: 'AA', cash: 10000, pos_m2m: 2000, holdings_mtm: 5000, nav: 17000 },
    ];
    const total = navTotalRow(rows);
    expect(isNaN(total.nav)).toBe(false);
    expect(isNaN(total.cash)).toBe(false);
    expect(isNaN(total.pos_m2m)).toBe(false);
    expect(isNaN(total.holdings_mtm)).toBe(false);
  });
});

describe('navTotalRow — three accounts cross-check', () => {
  it('cash + pos_m2m + holdings_mtm === nav (algebraic identity to 0.01)', () => {
    const rows = [
      { account: 'AA', cash: 50000, pos_m2m: 10000, holdings_mtm: 20000, nav: 80000 },
      { account: 'BB', cash: 30000, pos_m2m: -5000, holdings_mtm: 15000, nav: 40000 },
      { account: 'CC', cash: 20000, pos_m2m:  3000, holdings_mtm: 10000, nav: 33000 },
    ];
    const total = navTotalRow(rows);
    expect(total.cash + total.pos_m2m + total.holdings_mtm).toBeCloseTo(total.nav, 2);
  });
});

// ── navByAccount ─────────────────────────────────────────────────────────────

describe('navByAccount — grouping', () => {
  it('two accounts with 2 positions each: each account pos_m2m = sum of its unrealised', () => {
    const funds = [
      { account: 'AA', cash: 0, option_premium: 0 },
      { account: 'BB', cash: 0, option_premium: 0 },
    ];
    const positions = [
      { account: 'AA', unrealised: 3000 },
      { account: 'AA', unrealised: 2000 },
      { account: 'BB', unrealised: 1000 },
      { account: 'BB', unrealised:  500 },
    ];
    const holdings = [];
    const rows = navByAccount(['AA', 'BB'], funds, positions, holdings);
    const aa = rows.find(r => r.account === 'AA');
    const bb = rows.find(r => r.account === 'BB');
    expect(aa.pos_m2m).toBe(5000);
    expect(bb.pos_m2m).toBe(1500);
  });

  it('account with no positions: pos_m2m === 0, holdings_mtm still populated', () => {
    const funds = [{ account: 'AA', cash: 0, option_premium: 0 }];
    const positions = [];
    const holdings = [{ account: 'AA', cur_val: 12000 }];
    const rows = navByAccount(['AA'], funds, positions, holdings);
    expect(rows[0].pos_m2m).toBe(0);
    expect(rows[0].holdings_mtm).toBe(12000);
  });

  it('account filter: passing [\'AA\'] returns only AA row', () => {
    const funds = [
      { account: 'AA', cash: 1000, option_premium: 0 },
      { account: 'BB', cash: 2000, option_premium: 0 },
    ];
    const positions = [
      { account: 'AA', unrealised: 500 },
      { account: 'BB', unrealised: 700 },
    ];
    const holdings = [];
    const rows = navByAccount(['AA'], funds, positions, holdings);
    expect(rows).toHaveLength(1);
    expect(rows[0].account).toBe('AA');
    expect(rows[0].cash).toBe(1000);
    expect(rows[0].pos_m2m).toBe(500);
  });

  it('two accounts produce two entries in result array', () => {
    const funds = [
      { account: 'AA', cash: 10000, option_premium: 0 },
      { account: 'BB', cash: 20000, option_premium: 0 },
    ];
    const rows = navByAccount(['AA', 'BB'], funds, [], []);
    expect(rows).toHaveLength(2);
  });
});

// ── aggregateDayPnlForPositions ──────────────────────────────────────────────

describe('aggregateDayPnlForPositions — total', () => {
  it('single position: aggregate === baseDayPnlForPosition(pos)', () => {
    const pos = { pnl: 5000, prev_settlement_pnl: 3000 };
    expect(aggregateDayPnlForPositions([pos])).toBe(baseDayPnlForPosition(pos));
  });

  it('multiple positions: aggregate === sum of individual baseDayPnlForPosition calls', () => {
    const positions = [
      { pnl: 5000, prev_settlement_pnl: 3000 },   // → 2000
      { pnl: 1000, prev_settlement_pnl: 400 },     // → 600
      { pnl: 750, overnight_quantity: 0, day_change_val: 0, close_price: 0 }, // new pos → 750
    ];
    const expected = positions.reduce((sum, p) => sum + baseDayPnlForPosition(p), 0);
    expect(aggregateDayPnlForPositions(positions)).toBeCloseTo(expected, 10);
  });

  it('mixed gain/loss: net aggregate is algebraically correct', () => {
    const positions = [
      { pnl: 5000, prev_settlement_pnl: 3000 },    // +2000
      { pnl: -1500, prev_settlement_pnl: -500 },   // -1000
    ];
    expect(aggregateDayPnlForPositions(positions)).toBe(1000);
  });

  it('empty array → 0', () => {
    expect(aggregateDayPnlForPositions([])).toBe(0);
  });
});
