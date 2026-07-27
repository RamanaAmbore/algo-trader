/**
 * margin.test.js — test cash and funds math reachable via navByAccount.
 *
 * The broker funds row contains: `cash` (cash_sod) and `option_premium`.
 * navRowForAccount computes: cash_total = cash_sod + option_premium.
 * This is the only exported pure-function path for cash/funds math.
 *
 * Margin utilization (avail_margin, used_margin) is computed inline in
 * Svelte components from raw broker fields — it is NOT an exported
 * function and cannot be unit-tested here. These tests instead cover
 * every branch of the cash_total computation inside navRowForAccount,
 * which IS the critical math guard for the NavBreakdown SSOT (C pill).
 *
 * Tests cover:
 *   - Standard: cash + option_premium = cash_total
 *   - option_premium = 0: cash_total = cash only
 *   - option_premium missing / null / undefined → treated as 0
 *   - cash missing / null → treated as 0
 *   - Negative option_premium (net short options book)
 *   - Zero total (no funds, no premium)
 *   - Multiple accounts: cash sums independently
 *   - cash_total feeds nav correctly
 */

import { describe, it, expect } from 'vitest';
import { navByAccount, navTotalRow } from '$lib/data/nav.js';

// Helper: build a single-account nav row with only funds (no positions/holdings)
function cashRow(account, fundsFields = {}) {
  const funds = [{ account, ...fundsFields }];
  return navByAccount([account], funds, [], [])[0];
}

// ── cash_sod + option_premium = cash_total ────────────────────────────────────

describe('navByAccount — cash_total (cash + option_premium)', () => {
  it('standard: cash=100000, option_premium=5000 → cash=105000', () => {
    const row = cashRow('AA', { cash: 100000, option_premium: 5000 });
    expect(row.cash).toBe(105000);
  });

  it('option_premium=0: cash_total = cash only', () => {
    const row = cashRow('AA', { cash: 200000, option_premium: 0 });
    expect(row.cash).toBe(200000);
  });

  it('option_premium=null: treated as 0 (Number(null)||0)', () => {
    const row = cashRow('AA', { cash: 150000, option_premium: null });
    expect(row.cash).toBe(150000);
  });

  it('option_premium=undefined (field absent): treated as 0', () => {
    const row = cashRow('AA', { cash: 80000 });
    expect(row.cash).toBe(80000);
  });

  it('option_premium=NaN: treated as 0', () => {
    const row = cashRow('AA', { cash: 60000, option_premium: NaN });
    expect(row.cash).toBe(60000);
  });

  it('cash=null, option_premium=5000: cash_total = 5000 (null cash → 0)', () => {
    const row = cashRow('AA', { cash: null, option_premium: 5000 });
    expect(row.cash).toBe(5000);
  });

  it('cash=undefined (field absent), option_premium=3000 → cash_total=3000', () => {
    const row = cashRow('AA', { option_premium: 3000 });
    expect(row.cash).toBe(3000);
  });

  it('both cash and option_premium missing → cash_total = 0', () => {
    const row = cashRow('AA', {});
    expect(row.cash).toBe(0);
  });
});

// ── Negative option_premium (net short options book) ─────────────────────────

describe('navByAccount — negative option_premium', () => {
  it('negative premium: cash_total = cash + negative_premium', () => {
    // Net short options book: premium received offsets cash (negative option_premium
    // represents broker's MTM of short options liability)
    const row = cashRow('AA', { cash: 200000, option_premium: -20000 });
    expect(row.cash).toBe(180000);
  });

  it('premium exactly offsets cash: cash_total = 0', () => {
    const row = cashRow('AA', { cash: 50000, option_premium: -50000 });
    expect(row.cash).toBe(0);
  });

  it('premium exceeds cash: cash_total negative (over-leveraged options)', () => {
    const row = cashRow('AA', { cash: 30000, option_premium: -80000 });
    expect(row.cash).toBe(-50000);
  });
});

// ── Zero total margin ─────────────────────────────────────────────────────────

describe('navByAccount — zero total funds', () => {
  it('no funds row for account → cash_total = 0, nav = 0', () => {
    const rows = navByAccount(['AA'], [], [], []);
    expect(rows[0].cash).toBe(0);
    expect(rows[0].nav).toBe(0);
  });

  it('funds row for a different account → cash_total = 0', () => {
    const funds = [{ account: 'BB', cash: 100000, option_premium: 0 }];
    const rows = navByAccount(['AA'], funds, [], []);
    expect(rows[0].cash).toBe(0);
  });
});

// ── Multiple accounts — cash isolation ───────────────────────────────────────

describe('navByAccount — multi-account cash isolation', () => {
  it('two accounts: each gets its own cash_total', () => {
    const funds = [
      { account: 'AA', cash: 100000, option_premium: 5000 },
      { account: 'BB', cash: 200000, option_premium: 10000 },
    ];
    const rows = navByAccount(['AA', 'BB'], funds, [], []);
    const aa = rows.find(r => r.account === 'AA');
    const bb = rows.find(r => r.account === 'BB');
    expect(aa.cash).toBe(105000);
    expect(bb.cash).toBe(210000);
  });

  it('AA and BB: navTotalRow sums cash correctly', () => {
    const funds = [
      { account: 'AA', cash: 100000, option_premium: 5000 },
      { account: 'BB', cash: 200000, option_premium: 10000 },
    ];
    const rows = navByAccount(['AA', 'BB'], funds, [], []);
    const total = navTotalRow(rows);
    expect(total.cash).toBe(315000); // 105000 + 210000
  });

  it('only AA has a funds row: BB cash_total = 0', () => {
    const funds = [{ account: 'AA', cash: 50000, option_premium: 0 }];
    const rows = navByAccount(['AA', 'BB'], funds, [], []);
    const bb = rows.find(r => r.account === 'BB');
    expect(bb.cash).toBe(0);
  });
});

// ── cash_total feeds nav ──────────────────────────────────────────────────────

describe('navByAccount — cash_total contribution to nav', () => {
  it('nav = cash_total when no positions or holdings', () => {
    const row = cashRow('AA', { cash: 300000, option_premium: 25000 });
    expect(row.nav).toBe(325000);
  });

  it('nav includes cash + positions + holdings', () => {
    const funds = [{ account: 'AA', cash: 100000, option_premium: 5000 }];
    const positions = [{ account: 'AA', unrealised: 8000 }];
    const holdings = [{ account: 'AA', cur_val: 20000 }];
    const rows = navByAccount(['AA'], funds, positions, holdings);
    // cash=105000, pos_m2m=8000, holdings_mtm=20000 → nav=133000
    expect(rows[0].nav).toBe(133000);
  });
});

// ── Fractional cash values (sub-rupee broker rounding) ───────────────────────

describe('navByAccount — fractional cash values', () => {
  it('fractional cash and premium sum precisely', () => {
    const row = cashRow('AA', { cash: 1234.56, option_premium: 789.01 });
    // 1234.56 + 789.01 = 2023.57
    expect(row.cash).toBeCloseTo(2023.57, 2);
  });
});
