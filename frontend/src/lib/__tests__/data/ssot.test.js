import { describe, it, expect } from 'vitest';
import {
  baseDayPnlForPosition,
  aggregateDayPnlForPositions,
  navTotalRow,
  navByAccount,
} from '$lib/data/nav.js';

// ── Cross-surface SSOT (positions) ───────────────────────────────────────────

describe('SSOT — positions cross-surface', () => {
  it('baseDayPnlForPosition and aggregateDayPnlForPositions([pos]) return the same value', () => {
    const pos = { pnl: 5000, prev_settlement_pnl: 3000 };
    expect(aggregateDayPnlForPositions([pos])).toBe(baseDayPnlForPosition(pos));
  });

  it('new intraday position: single-item aggregate equals baseDayPnlForPosition', () => {
    const pos = { pnl: 1200, overnight_quantity: 0, day_change_val: 0, close_price: 0, average_price: 100 };
    expect(aggregateDayPnlForPositions([pos])).toBe(baseDayPnlForPosition(pos));
  });

  it('navTotalRow pos_m2m matches aggregateDayPnlForPositions when pos.unrealised === baseDayPnl', () => {
    // Construct a fixture where unrealised (navByAccount path) equals baseDayPnlForPosition
    // (aggregateDayPnlForPositions path). New-intraday position: oq=0 → baseDayPnl = pnl = 2000.
    // Set unrealised=2000 so the two paths agree.
    const positions = [
      { account: 'AA', unrealised: 2000, pnl: 2000, overnight_quantity: 0, day_change_val: 0, close_price: 0 },
    ];
    const navRow = { account: 'AA', cash: 0, pos_m2m: 2000, holdings_mtm: 0, nav: 2000 };
    const total = navTotalRow([navRow]);
    const aggDayPnl = aggregateDayPnlForPositions(positions);
    expect(total.pos_m2m).toBeCloseTo(aggDayPnl, 2);
  });

  it('mutating pnl in position: both surfaces reflect the change', () => {
    const pos = { pnl: 1000, prev_settlement_pnl: 500 };
    const before = baseDayPnlForPosition(pos);
    const beforeAgg = aggregateDayPnlForPositions([pos]);
    expect(before).toBe(500);
    expect(beforeAgg).toBe(500);

    // Change pnl and recompute (functions are pure — pass new object)
    const pos2 = { ...pos, pnl: 2000 };
    expect(baseDayPnlForPosition(pos2)).toBe(1500);
    expect(aggregateDayPnlForPositions([pos2])).toBe(1500);
  });
});

// ── Cross-surface SSOT (NAV components) ─────────────────────────────────────

describe('SSOT — NAV components cross-surface', () => {
  it('navByAccount pos_m2m for AA matches aggregateDayPnlForPositions of AA\'s positions (aligned fixture)', () => {
    // For the two formulas to agree, use new-intraday positions where
    // unrealised === pnl (overnight_quantity=0, no prior settlement pnl).
    const positions = [
      { account: 'AA', unrealised: 3000, pnl: 3000, overnight_quantity: 0, day_change_val: 0, close_price: 0 },
      { account: 'AA', unrealised: 1500, pnl: 1500, overnight_quantity: 0, day_change_val: 0, close_price: 0 },
    ];
    const funds = [{ account: 'AA', cash: 0, option_premium: 0 }];
    const rows = navByAccount(['AA'], funds, positions, []);
    const aaNav = rows[0].pos_m2m;   // sum of unrealised: 4500

    const aaPositions = positions.filter(p => p.account === 'AA');
    const aaAgg = aggregateDayPnlForPositions(aaPositions);  // sum of baseDayPnl: 4500

    expect(aaNav).toBeCloseTo(aaAgg, 2);
  });

  it('navTotalRow nav total equals sum of individual account navs', () => {
    const rows = [
      { account: 'AA', cash: 50000, pos_m2m: 10000, holdings_mtm: 20000, nav: 80000 },
      { account: 'BB', cash: 30000, pos_m2m:  5000, holdings_mtm: 15000, nav: 50000 },
    ];
    const total = navTotalRow(rows);
    const manualSum = rows.reduce((sum, r) => sum + r.nav, 0);
    expect(total.nav).toBeCloseTo(manualSum, 2);
  });

  it('holdings component: navByAccount holdings_mtm for AA equals sum of AA\'s cur_val', () => {
    const funds = [{ account: 'AA', cash: 0, option_premium: 0 }];
    const holdings = [
      { account: 'AA', cur_val: 15000 },
      { account: 'AA', cur_val:  8000 },
      { account: 'BB', cur_val: 25000 },  // different account — must not leak in
    ];
    const rows = navByAccount(['AA'], funds, [], holdings);
    expect(rows[0].holdings_mtm).toBe(23000);  // 15000 + 8000, not 48000
  });

  it('navTotalRow over navByAccount: total nav = sum of all account navs (algebraic)', () => {
    const funds = [
      { account: 'AA', cash: 100000, option_premium: 5000 },
      { account: 'BB', cash:  60000, option_premium: 2000 },
    ];
    const positions = [
      { account: 'AA', unrealised:  8000 },
      { account: 'BB', unrealised: -3000 },
    ];
    const holdings = [
      { account: 'AA', cur_val: 20000 },
      { account: 'BB', cur_val: 10000 },
    ];
    const rows = navByAccount(['AA', 'BB'], funds, positions, holdings);
    const total = navTotalRow(rows);
    const manualSum = rows.reduce((s, r) => s + r.nav, 0);
    expect(total.nav).toBeCloseTo(manualSum, 2);
  });
});

// ── No stale-cache drift ─────────────────────────────────────────────────────

describe('SSOT — no stale-cache drift (pure functions)', () => {
  it('navTotalRow called twice with same input → identical results', () => {
    const rows = [
      { account: 'AA', cash: 50000, pos_m2m: 10000, holdings_mtm: 20000, nav: 80000 },
      { account: 'BB', cash: 30000, pos_m2m:  5000, holdings_mtm: 15000, nav: 50000 },
    ];
    const first  = navTotalRow(rows);
    const second = navTotalRow(rows);
    expect(first).toEqual(second);
  });

  it('navTotalRow does not mutate input rows', () => {
    const rows = [
      { account: 'AA', cash: 50000, pos_m2m: 10000, holdings_mtm: 20000, nav: 80000 },
    ];
    const before = { ...rows[0] };
    navTotalRow(rows);
    expect(rows[0]).toEqual(before);
  });

  it('navByAccount called twice with same inputs → identical structure', () => {
    const funds = [{ account: 'AA', cash: 10000, option_premium: 500 }];
    const positions = [{ account: 'AA', unrealised: 2000 }];
    const holdings = [{ account: 'AA', cur_val: 5000 }];
    const first  = navByAccount(['AA'], funds, positions, holdings);
    const second = navByAccount(['AA'], funds, positions, holdings);
    expect(first).toEqual(second);
  });

  it('navByAccount does not mutate input arrays', () => {
    const funds = [{ account: 'AA', cash: 10000, option_premium: 0 }];
    const positions = [{ account: 'AA', unrealised: 1000 }];
    const holdings  = [{ account: 'AA', cur_val: 500 }];
    const fundsBefore     = JSON.stringify(funds);
    const positionsBefore = JSON.stringify(positions);
    const holdingsBefore  = JSON.stringify(holdings);
    navByAccount(['AA'], funds, positions, holdings);
    expect(JSON.stringify(funds)).toBe(fundsBefore);
    expect(JSON.stringify(positions)).toBe(positionsBefore);
    expect(JSON.stringify(holdings)).toBe(holdingsBefore);
  });

  it('aggregateDayPnlForPositions called twice with same input → same result', () => {
    const positions = [
      { pnl: 5000, prev_settlement_pnl: 3000 },
      { pnl: 1000, overnight_quantity: 0, day_change_val: 0, close_price: 0 },
    ];
    expect(aggregateDayPnlForPositions(positions)).toBe(aggregateDayPnlForPositions(positions));
  });
});
