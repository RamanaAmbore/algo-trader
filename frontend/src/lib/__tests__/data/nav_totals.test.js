/**
 * nav_totals.test.js — deeper coverage of navTotalRow (pure row-summation
 * over already-server-computed rows). Complements nav.test.js.
 *
 * navByAccount/navRowForAccount — the per-account NAV FORMULA this file
 * used to test — were removed 2026-09 (NAV SSOT consolidation). That
 * formula now lives exclusively in backend/api/algo/nav.py:compute_firm_nav()
 * and is served via GET /api/nav/by-account; the equivalent coverage
 * (multiple rows per account, null/string coercion, mixed-sign, 3-account
 * cross-check) now lives in backend/tests/test_nav_by_account.py.
 *
 * This file adds:
 *   - navTotalRow with negative pos_m2m (loss positions)
 *   - navTotalRow: nav sum cross-check (total.nav = sum of all row navs)
 */

import { describe, it, expect } from 'vitest';
import { navTotalRow } from '$lib/data/nav.js';

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
