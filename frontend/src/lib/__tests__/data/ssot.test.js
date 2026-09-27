import { describe, it, expect } from 'vitest';
import {
  baseDayPnlForPosition,
  aggregateDayPnlForPositions,
  navTotalRow,
} from '$lib/data/nav.js';
// navByAccount removed 2026-09 (NAV SSOT consolidation) — the per-account
// NAV formula it duplicated now lives exclusively in
// backend/api/algo/nav.py:compute_firm_nav(), served via
// GET /api/nav/by-account. Where this file previously called navByAccount
// to build a nav-row fixture for a cross-surface check, it now constructs
// the equivalent row by hand (the fixture's SHAPE, not the removed
// formula, is what those tests exercise) — the formula itself is covered
// in backend/tests/test_nav_by_account.py.

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
    // New-intraday position: oq=0 → baseDayPnl = pnl = 2000. Construct the
    // nav row by hand with pos_m2m set to the same value so the two
    // (independently-sourced) totals agree.
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
  it('a nav-by-account row\'s pos_m2m matches aggregateDayPnlForPositions of the same account\'s positions (aligned fixture)', () => {
    // For the two independently-sourced numbers to agree, use
    // new-intraday positions where unrealised === pnl (overnight_quantity=0,
    // no prior settlement pnl) — same alignment condition the backend's
    // by_account breakdown and the frontend's Day P&L formula both rely on.
    const positions = [
      { account: 'AA', unrealised: 3000, pnl: 3000, overnight_quantity: 0, day_change_val: 0, close_price: 0 },
      { account: 'AA', unrealised: 1500, pnl: 1500, overnight_quantity: 0, day_change_val: 0, close_price: 0 },
    ];
    const aaNav = positions.reduce((s, p) => s + p.unrealised, 0);   // 4500 (server-side sum)

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

  it('navTotalRow over server-computed rows: total nav = sum of all account navs (algebraic)', () => {
    const rows = [
      { account: 'AA', cash: 105000, pos_m2m:  8000, holdings_mtm: 20000, nav: 133000 },
      { account: 'BB', cash:  62000, pos_m2m: -3000, holdings_mtm: 10000, nav:  69000 },
    ];
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

  it('aggregateDayPnlForPositions called twice with same input → same result', () => {
    const positions = [
      { pnl: 5000, prev_settlement_pnl: 3000 },
      { pnl: 1000, overnight_quantity: 0, day_change_val: 0, close_price: 0 },
    ];
    expect(aggregateDayPnlForPositions(positions)).toBe(aggregateDayPnlForPositions(positions));
  });
});
