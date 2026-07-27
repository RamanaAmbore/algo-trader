/**
 * nav_totals.test.js — deeper coverage of navTotalRow and navByAccount
 * that complements the existing nav.test.js.
 *
 * nav.test.js already covers:
 *   - navTotalRow: basic 2-row sum, empty→null, null→null, single row
 *   - navByAccount: single account, empty accounts, missing funds row
 *
 * This file adds:
 *   - navTotalRow with negative pos_m2m (loss positions)
 *   - navTotalRow: nav sum cross-check (total.nav = sum of all row navs)
 *   - navByAccount: multiple position rows per account (reduce sums correctly)
 *   - navByAccount: option_premium=null falls through to 0
 *   - navByAccount: account with no positions (pos_m2m = 0)
 *   - navByAccount: 3-account scenario (total nav = per-account sum)
 *   - navByAccount: mixed sign positions (gain + loss accounts)
 *   - navByAccount: string number fields coerced via Number()
 */

import { describe, it, expect } from 'vitest';
import { navByAccount, navTotalRow } from '$lib/data/nav.js';

// ── navTotalRow — negative pos_m2m (loss positions) ──────────────────────────

describe('navTotalRow — negative pos_m2m', () => {
  it('account in loss: pos_m2m negative, total reflects net', () => {
    const rows = [
      { account: 'AA', cash: 100000, pos_m2m: -15000, holdings_mtm: 0, nav: 85000 },
      { account: 'BB', cash: 50000,  pos_m2m:  10000, holdings_mtm: 0, nav: 60000 },
    ];
    const total = navTotalRow(rows);
    expect(total.pos_m2m).toBe(-5000);
    expect(total.nav).toBe(145000);
  });

  it('all accounts in loss: total pos_m2m is negative', () => {
    const rows = [
      { account: 'AA', cash: 200000, pos_m2m: -30000, holdings_mtm: 0, nav: 170000 },
      { account: 'BB', cash: 100000, pos_m2m: -20000, holdings_mtm: 0, nav: 80000 },
    ];
    const total = navTotalRow(rows);
    expect(total.pos_m2m).toBe(-50000);
    expect(total.cash).toBe(300000);
    expect(total.nav).toBe(250000);
  });
});

// ── navTotalRow — nav cross-check ────────────────────────────────────────────

describe('navTotalRow — nav cross-check (total.nav = sum of row navs)', () => {
  it('3 accounts: total.nav equals sum of individual navs', () => {
    const rows = [
      { account: 'AA', cash: 50000,  pos_m2m:  5000, holdings_mtm: 10000, nav:  65000 },
      { account: 'BB', cash: 80000,  pos_m2m: -2000, holdings_mtm: 20000, nav:  98000 },
      { account: 'CC', cash: 30000,  pos_m2m:  3000, holdings_mtm:  5000, nav:  38000 },
    ];
    const total = navTotalRow(rows);
    const summedNav = rows.reduce((s, r) => s + r.nav, 0);
    expect(total.nav).toBe(summedNav);
    expect(total.nav).toBe(201000);
  });

  it('total.cash + total.pos_m2m + total.holdings_mtm === total.nav', () => {
    const rows = [
      { account: 'AA', cash: 100000, pos_m2m: 10000, holdings_mtm: 20000, nav: 130000 },
      { account: 'BB', cash: 50000,  pos_m2m:  5000, holdings_mtm: 15000, nav:  70000 },
    ];
    const total = navTotalRow(rows);
    expect(total.cash + total.pos_m2m + total.holdings_mtm).toBe(total.nav);
  });
});

// ── navByAccount — multiple positions per account ─────────────────────────────

describe('navByAccount — multiple positions rows per account', () => {
  it('two position rows for same account: pos_m2m = sum of unrealised', () => {
    const positions = [
      { account: 'AA', unrealised: 5000 },
      { account: 'AA', unrealised: 3000 },
    ];
    const rows = navByAccount(['AA'], [], positions, []);
    expect(rows[0].pos_m2m).toBe(8000);
  });

  it('three positions including a loss: pos_m2m = net sum', () => {
    const positions = [
      { account: 'AA', unrealised:  8000 },
      { account: 'AA', unrealised: -3000 },
      { account: 'AA', unrealised:  1000 },
    ];
    const rows = navByAccount(['AA'], [], positions, []);
    expect(rows[0].pos_m2m).toBe(6000);
  });

  it('positions from BB do not pollute AA', () => {
    const positions = [
      { account: 'AA', unrealised: 4000 },
      { account: 'BB', unrealised: 9000 },
    ];
    const rows = navByAccount(['AA', 'BB'], [], positions, []);
    const aa = rows.find(r => r.account === 'AA');
    const bb = rows.find(r => r.account === 'BB');
    expect(aa.pos_m2m).toBe(4000);
    expect(bb.pos_m2m).toBe(9000);
  });
});

// ── navByAccount — option_premium = null ─────────────────────────────────────

describe('navByAccount — option_premium=null falls through to 0', () => {
  it('option_premium=null: cash_total = cash (null treated as 0)', () => {
    const funds = [{ account: 'AA', cash: 200000, option_premium: null }];
    const rows = navByAccount(['AA'], funds, [], []);
    expect(rows[0].cash).toBe(200000);
  });

  it('both cash and option_premium null → cash=0, nav=0', () => {
    const funds = [{ account: 'AA', cash: null, option_premium: null }];
    const rows = navByAccount(['AA'], funds, [], []);
    expect(rows[0].cash).toBe(0);
    expect(rows[0].nav).toBe(0);
  });
});

// ── navByAccount — account with no positions ──────────────────────────────────

describe('navByAccount — account with no positions', () => {
  it('account has funds and holdings but no positions → pos_m2m = 0', () => {
    const funds = [{ account: 'AA', cash: 100000, option_premium: 0 }];
    const holdings = [{ account: 'AA', cur_val: 30000 }];
    const rows = navByAccount(['AA'], funds, [], holdings);
    expect(rows[0].pos_m2m).toBe(0);
    expect(rows[0].nav).toBe(130000);
  });

  it('account has cash but zero positions and holdings → nav = cash', () => {
    const funds = [{ account: 'AA', cash: 75000, option_premium: 5000 }];
    const rows = navByAccount(['AA'], funds, [], []);
    expect(rows[0].nav).toBe(80000);
  });
});

// ── navByAccount — 3-account: total nav = per-account sum ────────────────────

describe('navByAccount — 3-account total NAV cross-check', () => {
  it('total nav from navTotalRow equals sum of navByAccount rows', () => {
    const funds = [
      { account: 'AA', cash: 100000, option_premium: 5000 },
      { account: 'BB', cash:  80000, option_premium:    0 },
      { account: 'CC', cash:  50000, option_premium: 2000 },
    ];
    const positions = [
      { account: 'AA', unrealised:  10000 },
      { account: 'BB', unrealised:  -5000 },
      { account: 'CC', unrealised:   3000 },
    ];
    const holdings = [
      { account: 'AA', cur_val: 20000 },
      { account: 'CC', cur_val: 15000 },
    ];
    const rows = navByAccount(['AA', 'BB', 'CC'], funds, positions, holdings);
    const total = navTotalRow(rows);
    const expectedSum = rows.reduce((s, r) => s + r.nav, 0);
    expect(total.nav).toBe(expectedSum);
    // AA: 105000 + 10000 + 20000 = 135000
    // BB:  80000 -  5000 +     0 =  75000
    // CC:  52000 +  3000 + 15000 =  70000
    expect(total.nav).toBe(280000);
  });
});

// ── navByAccount — mixed sign positions (gain + loss accounts) ────────────────

describe('navByAccount — mixed sign pos_m2m across accounts', () => {
  it('one account profitable, one in loss: total reflects net', () => {
    const positions = [
      { account: 'AA', unrealised:  20000 },
      { account: 'BB', unrealised: -12000 },
    ];
    const rows = navByAccount(['AA', 'BB'], [], positions, []);
    const total = navTotalRow(rows);
    expect(total.pos_m2m).toBe(8000);
  });
});

// ── navByAccount — string number fields coerced via Number() ─────────────────

describe('navByAccount — string numeric fields coercion', () => {
  it('unrealised as string → coerced to number', () => {
    const positions = /** @type {any} */ ([{ account: 'AA', unrealised: '7500' }]);
    const rows = navByAccount(['AA'], [], positions, []);
    expect(rows[0].pos_m2m).toBe(7500);
  });

  it('cur_val as string → coerced to number', () => {
    const holdings = /** @type {any} */ ([{ account: 'AA', cur_val: '25000' }]);
    const rows = navByAccount(['AA'], [], [], holdings);
    expect(rows[0].holdings_mtm).toBe(25000);
  });

  it('cash as string → coerced to number', () => {
    const funds = /** @type {any} */ ([{ account: 'AA', cash: '100000', option_premium: '5000' }]);
    const rows = navByAccount(['AA'], funds, [], []);
    expect(rows[0].cash).toBe(105000);
  });
});
