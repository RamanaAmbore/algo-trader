/**
 * holdings.test.js — test the holdings aggregation paths reachable via
 * navByAccount / navRowForAccount (the only exported pure functions that
 * process holdings rows).
 *
 * The broker supplies a `cur_val` field (qty × LTP) per holding row.
 * navRowForAccount sums `cur_val` per account into `holdings_mtm`.
 * There is no separate exported "holdings day P&L" function — holdings
 * day MTM is tracked via NavBreakdown, not a standalone helper.
 *
 * Tests here exercise:
 *   - Standard holdings aggregation via navByAccount
 *   - Zero / missing cur_val guard (Number() || 0)
 *   - Multiple holdings rows per account reduce correctly
 *   - Holdings from one account do NOT bleed into another
 *   - Empty holdings array → holdings_mtm = 0
 *   - null / undefined cur_val → treated as 0
 */

import { describe, it, expect } from 'vitest';
import { navByAccount, navTotalRow } from '$lib/data/nav.js';

// Helpers to build minimal data fixtures
function makeHolding(account, cur_val) {
  return { account, cur_val };
}

function makeNav(account, options = {}) {
  return navByAccount(
    [account],
    options.funds ?? [],
    options.positions ?? [],
    options.holdings ?? [],
  )[0];
}

// ── Standard holdings aggregation ───────────────────────────────────────────

describe('navByAccount — holdings aggregation (holdings_mtm)', () => {
  it('single holding row: holdings_mtm = cur_val', () => {
    const row = makeNav('AA', { holdings: [makeHolding('AA', 50000)] });
    expect(row.holdings_mtm).toBe(50000);
  });

  it('two holding rows for same account: holdings_mtm = sum', () => {
    const holdings = [makeHolding('AA', 30000), makeHolding('AA', 20000)];
    const row = makeNav('AA', { holdings });
    expect(row.holdings_mtm).toBe(50000);
  });

  it('three holding rows: holdings_mtm = correct sum', () => {
    const holdings = [
      makeHolding('AA', 10000),
      makeHolding('AA', 25000),
      makeHolding('AA', 15000),
    ];
    const row = makeNav('AA', { holdings });
    expect(row.holdings_mtm).toBe(50000);
  });

  it('empty holdings array → holdings_mtm = 0', () => {
    const row = makeNav('AA', { holdings: [] });
    expect(row.holdings_mtm).toBe(0);
  });
});

// ── Zero / null / undefined cur_val guards ───────────────────────────────────

describe('navByAccount — holdings with zero or missing cur_val', () => {
  it('cur_val = 0 → holdings_mtm = 0', () => {
    const row = makeNav('AA', { holdings: [makeHolding('AA', 0)] });
    expect(row.holdings_mtm).toBe(0);
  });

  it('cur_val = null → treated as 0 (Number(null)||0)', () => {
    const row = makeNav('AA', { holdings: [{ account: 'AA', cur_val: null }] });
    expect(row.holdings_mtm).toBe(0);
  });

  it('cur_val = undefined → treated as 0', () => {
    const row = makeNav('AA', { holdings: [{ account: 'AA' }] });
    expect(row.holdings_mtm).toBe(0);
  });

  it('cur_val = NaN → treated as 0 (Number(NaN)||0)', () => {
    const row = makeNav('AA', { holdings: [{ account: 'AA', cur_val: NaN }] });
    expect(row.holdings_mtm).toBe(0);
  });

  it('mixed valid + null cur_val: only valid rows contribute', () => {
    const holdings = [
      { account: 'AA', cur_val: 40000 },
      { account: 'AA', cur_val: null },
      { account: 'AA', cur_val: 10000 },
    ];
    const row = makeNav('AA', { holdings });
    expect(row.holdings_mtm).toBe(50000);
  });
});

// ── Account isolation ────────────────────────────────────────────────────────

describe('navByAccount — holdings do not bleed across accounts', () => {
  it('AA holding rows do not appear in BB account', () => {
    const holdings = [
      makeHolding('AA', 30000),
      makeHolding('BB', 20000),
    ];
    const rows = navByAccount(['AA', 'BB'], [], [], holdings);
    const aa = rows.find(r => r.account === 'AA');
    const bb = rows.find(r => r.account === 'BB');
    expect(aa.holdings_mtm).toBe(30000);
    expect(bb.holdings_mtm).toBe(20000);
  });

  it('account with no matching holding rows → holdings_mtm = 0', () => {
    const holdings = [makeHolding('AA', 50000)];
    const rows = navByAccount(['AA', 'BB'], [], [], holdings);
    const bb = rows.find(r => r.account === 'BB');
    expect(bb.holdings_mtm).toBe(0);
  });

  it('holdings from unknown account are ignored by all named accounts', () => {
    const holdings = [makeHolding('ZZ', 999999)]; // not in accounts list
    const rows = navByAccount(['AA', 'BB'], [], [], holdings);
    rows.forEach(r => expect(r.holdings_mtm).toBe(0));
  });
});

// ── Holdings contribution to nav ─────────────────────────────────────────────

describe('navByAccount — holdings_mtm feeds nav correctly', () => {
  it('nav = cash + pos_m2m + holdings_mtm (holdings path)', () => {
    const funds = [{ account: 'AA', cash: 100000, option_premium: 0 }];
    const positions = [{ account: 'AA', unrealised: 5000 }];
    const holdings = [makeHolding('AA', 20000)];
    const row = makeNav('AA', { funds, positions, holdings });
    expect(row.nav).toBe(100000 + 5000 + 20000);
  });

  it('large holdings portfolio: nav dominated by holdings_mtm', () => {
    const funds = [{ account: 'AA', cash: 10000, option_premium: 0 }];
    const holdings = [
      makeHolding('AA', 500000),
      makeHolding('AA', 300000),
    ];
    const row = makeNav('AA', { funds, holdings });
    expect(row.holdings_mtm).toBe(800000);
    expect(row.nav).toBe(810000);
  });

  it('navTotalRow sums holdings_mtm across two accounts', () => {
    const holdings = [makeHolding('AA', 30000), makeHolding('BB', 20000)];
    const rows = navByAccount(['AA', 'BB'], [], [], holdings);
    const total = navTotalRow(rows);
    expect(total.holdings_mtm).toBe(50000);
  });
});

// ── Negative cur_val (short holding / MTM loss) ───────────────────────────────

describe('navByAccount — negative cur_val edge', () => {
  it('negative cur_val (e.g. pledged/margin position) reduces holdings_mtm', () => {
    const holdings = [
      makeHolding('AA', 50000),
      makeHolding('AA', -5000),
    ];
    const row = makeNav('AA', { holdings });
    expect(row.holdings_mtm).toBe(45000);
  });
});
