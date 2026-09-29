/**
 * fundsAggregate.test.js — Vitest unit tests for
 * $lib/data/fundsAggregate.js's sumMarginAvail / sumMarginTotal /
 * sumLiveCashTotal.
 *
 * Real-money fix (2026-09) — NavStrip "₹0 margin for an extended period,
 * then jumps to correct value" incident. portfolioStore.svelte.js's
 * _marginAvail / _marginTotal / _liveCashTotal used to freeze the WHOLE
 * cross-account total the moment ANY single account went degraded
 * (backend-tagged stale_accounts substitution — common, one flaky Dhan/
 * Groww account). On a fresh page load the remembered freeze-scalar reset
 * to 0; if the first poll(s) landed while degraded, the getter returned 0
 * and never recovered until a non-degraded poll arrived, even though the
 * real numbers for every healthy account were already sitting in
 * fundRows. Fixed by hoisting the sum logic into these pure, directly-
 * testable helpers (portfolioStore.svelte.js itself can't be imported
 * under Vitest — see portfolioStore.test.js's own header for why).
 *
 * Five quality dimensions:
 *  1. SSOT   — imports and exercises the REAL shipped helper functions
 *              (not a hand-copied mirror), unlike portfolioStore.test.js's
 *              necessary mirror-function workaround for the runes file.
 *  2. Perf   — pure synchronous arithmetic, no I/O.
 *  3. Stale  — the five required cases below specifically distinguish
 *              "exclude stale accounts" from "include stale accounts'
 *              own last-known-good values" — the exact design decision
 *              this fix hinges on.
 *  4. Reuse  — one parametrised describe.each covers all three exported
 *              helpers (they share the same null/TOTAL/degraded-row shape).
 *  5. UX     — case 1 (null on no-poll-yet) is what PositionStrip's
 *              fmtMoney renders as '—' instead of '₹0' — the operator-
 *              facing half of this fix.
 */

import { describe, it, expect } from 'vitest';
import { sumMarginAvail, sumMarginTotal, sumLiveCashTotal } from '$lib/data/fundsAggregate.js';

describe('sumMarginAvail', () => {
  it('returns null when fundRows is null (no poll has ever landed — genuinely unknown, not 0)', () => {
    expect(sumMarginAvail(null)).toBeNull();
  });

  it('returns null when fundRows is an empty array', () => {
    expect(sumMarginAvail([])).toBeNull();
  });

  it('all-healthy rows sum correctly', () => {
    const rows = [
      { account: 'ZG0790', avail_margin: 10_000 },
      { account: 'DH6847', avail_margin: 5_000 },
    ];
    expect(sumMarginAvail(rows)).toBe(15_000);
  });

  it('mixed healthy + stale rows carrying REAL non-zero last-known-good values — sum includes ALL rows, not just the healthy ones', () => {
    // This is the case that actually distinguishes "include stale
    // accounts" (the fix) from "exclude stale accounts" (the old,
    // implicit behaviour when the whole scalar froze instead) — every
    // one of these four rows carries a genuine non-zero number.
    const rows = [
      { account: 'ZG0790', avail_margin: 10_000, account_stale: false },
      { account: 'DH6847', avail_margin: 5_000,  account_stale: false },
      { account: 'ZJ6294', avail_margin: 8_500,  account_stale: true }, // stale but real LKG value
      { account: 'GRW001', avail_margin: 2_250,  account_stale: true }, // stale but real LKG value
    ];
    expect(sumMarginAvail(rows)).toBe(10_000 + 5_000 + 8_500 + 2_250);
  });

  it('stale rows carrying a genuine 0 balance — sum matches what excluding them would also produce (regression documents the two designs coincide here)', () => {
    const rows = [
      { account: 'ZG0790', avail_margin: 10_000, account_stale: false },
      { account: 'DH6847', avail_margin: 0,       account_stale: true },
    ];
    expect(sumMarginAvail(rows)).toBe(10_000);
  });

  it('a confirmed real 0 on a healthy (non-stale) account is included as 0 (missing-vs-zero preserved, not dropped)', () => {
    const rows = [
      { account: 'ZG0790', avail_margin: 0, account_stale: false },
      { account: 'DH6847', avail_margin: 5_000, account_stale: false },
    ];
    expect(sumMarginAvail(rows)).toBe(5_000);
  });

  it('excludes a TOTAL row (case-insensitive) from the sum — the backend may include its own pre-summed total row', () => {
    const rows = [
      { account: 'ZG0790', avail_margin: 10_000 },
      { account: 'DH6847', avail_margin: 5_000 },
      { account: 'TOTAL',  avail_margin: 15_000 },
      { account: 'total',  avail_margin: 15_000 },
    ];
    expect(sumMarginAvail(rows)).toBe(15_000);
  });

  it('missing avail_margin field on a row falls back to 0 (existing Number(x||0) convention, unchanged)', () => {
    const rows = [{ account: 'ZG0790' }, { account: 'DH6847', avail_margin: 5_000 }];
    expect(sumMarginAvail(rows)).toBe(5_000);
  });
});

describe('sumMarginTotal', () => {
  it('returns null when fundRows is null', () => {
    expect(sumMarginTotal(null)).toBeNull();
  });

  it('returns null when fundRows is an empty array', () => {
    expect(sumMarginTotal([])).toBeNull();
  });

  it('all-healthy rows sum used + avail correctly', () => {
    const rows = [
      { account: 'ZG0790', used_margin: 2_000, avail_margin: 10_000 },
      { account: 'DH6847', used_margin: 1_000, avail_margin: 5_000 },
    ];
    expect(sumMarginTotal(rows)).toBe(2_000 + 10_000 + 1_000 + 5_000);
  });

  it('mixed healthy + stale rows with real non-zero LKG values — sum includes all rows', () => {
    const rows = [
      { account: 'ZG0790', used_margin: 2_000, avail_margin: 10_000, account_stale: false },
      { account: 'DH6847', used_margin: 1_000, avail_margin: 5_000,  account_stale: false },
      { account: 'ZJ6294', used_margin: 500,   avail_margin: 8_500,  account_stale: true },
      { account: 'GRW001', used_margin: 100,   avail_margin: 2_250,  account_stale: true },
    ];
    expect(sumMarginTotal(rows)).toBe(2_000 + 10_000 + 1_000 + 5_000 + 500 + 8_500 + 100 + 2_250);
  });

  it('stale rows carrying genuine 0 balances — sum matches the exclude-stale design too', () => {
    const rows = [
      { account: 'ZG0790', used_margin: 2_000, avail_margin: 10_000, account_stale: false },
      { account: 'DH6847', used_margin: 0,     avail_margin: 0,      account_stale: true },
    ];
    expect(sumMarginTotal(rows)).toBe(12_000);
  });

  it('a confirmed real 0 on a healthy account is included as 0', () => {
    const rows = [
      { account: 'ZG0790', used_margin: 0, avail_margin: 0, account_stale: false },
      { account: 'DH6847', used_margin: 1_000, avail_margin: 5_000, account_stale: false },
    ];
    expect(sumMarginTotal(rows)).toBe(6_000);
  });

  it('excludes a TOTAL row from the sum', () => {
    const rows = [
      { account: 'ZG0790', used_margin: 2_000, avail_margin: 10_000 },
      { account: 'TOTAL',  used_margin: 2_000, avail_margin: 10_000 },
    ];
    expect(sumMarginTotal(rows)).toBe(12_000);
  });
});

describe('sumLiveCashTotal', () => {
  it('returns null when fundRows is null', () => {
    expect(sumLiveCashTotal(null)).toBeNull();
  });

  it('returns null when fundRows is an empty array', () => {
    expect(sumLiveCashTotal([])).toBeNull();
  });

  it('all-healthy rows sum live_cash correctly', () => {
    const rows = [
      { account: 'ZG0790', live_cash: 20_000 },
      { account: 'DH6847', live_cash: 8_000 },
    ];
    expect(sumLiveCashTotal(rows)).toBe(28_000);
  });

  it('mixed healthy + stale rows with real non-zero LKG values — sum includes all rows', () => {
    const rows = [
      { account: 'ZG0790', live_cash: 20_000, account_stale: false },
      { account: 'DH6847', live_cash: 8_000,  account_stale: false },
      { account: 'ZJ6294', live_cash: 12_000, account_stale: true },
      { account: 'GRW001', live_cash: 3_400,  account_stale: true },
    ];
    expect(sumLiveCashTotal(rows)).toBe(20_000 + 8_000 + 12_000 + 3_400);
  });

  it('stale rows carrying genuine 0 balances — sum matches the exclude-stale design too', () => {
    const rows = [
      { account: 'ZG0790', live_cash: 20_000, account_stale: false },
      { account: 'DH6847', live_cash: 0,       account_stale: true },
    ];
    expect(sumLiveCashTotal(rows)).toBe(20_000);
  });

  it('a confirmed real 0 on a healthy account is included as 0', () => {
    const rows = [
      { account: 'ZG0790', live_cash: 0, account_stale: false },
      { account: 'DH6847', live_cash: 8_000, account_stale: false },
    ];
    expect(sumLiveCashTotal(rows)).toBe(8_000);
  });

  it('falls back to `cash` when live_cash is 0/unset (backend has not surfaced live_cash yet)', () => {
    const rows = [
      { account: 'ZG0790', live_cash: 0, cash: 4_500 },
      { account: 'DH6847', live_cash: 8_000, cash: 1_000 },
    ];
    // ZG0790: live_cash is 0 → falls back to cash (4,500).
    // DH6847: live_cash is non-zero → uses live_cash directly (8,000).
    expect(sumLiveCashTotal(rows)).toBe(4_500 + 8_000);
  });

  it('excludes a TOTAL row from the sum', () => {
    const rows = [
      { account: 'ZG0790', live_cash: 20_000 },
      { account: 'TOTAL',  live_cash: 20_000 },
    ];
    expect(sumLiveCashTotal(rows)).toBe(20_000);
  });
});
