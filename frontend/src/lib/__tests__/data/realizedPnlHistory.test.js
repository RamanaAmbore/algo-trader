/**
 * realizedPnlHistory.test.js — Vitest unit tests for
 * $lib/data/realizedPnlHistory.js, the pure helpers backing the new
 * "Realized P&L by Expiry" tab on /admin/history.
 *
 * Five quality dimensions:
 *  1. SSOT   — imports and exercises the real shipped helper functions.
 *  2. Perf   — pure synchronous arithmetic/string logic, no I/O.
 *  3. Stale  — buildRealizedPnlParams never emits an undefined/empty
 *              key; normalizeRealizedPnlRows never throws on a
 *              malformed/absent response (the one likely contract-drift
 *              shape between the agreed backend contract and whatever
 *              ships).
 *  4. Reuse  — one param builder, one normalizer, one summer — matches
 *              the fundsAggregate.js precedent of pure helpers lifted
 *              out of a .svelte.js file so they're directly testable.
 *  5. UX     — sumRealizedPnl skips a bad row instead of poisoning the
 *              whole "Total realized" pill with NaN.
 */

import { describe, it, expect } from 'vitest';
import {
  buildRealizedPnlParams, normalizeRealizedPnlRows, sumRealizedPnl,
} from '$lib/data/realizedPnlHistory.js';

describe('buildRealizedPnlParams', () => {
  it('returns an empty object when no filters are set', () => {
    expect(buildRealizedPnlParams({})).toEqual({});
    expect(buildRealizedPnlParams()).toEqual({});
  });

  it('omits undefined, null, empty-string and whitespace-only filters', () => {
    const out = buildRealizedPnlParams({
      account: '', segment: undefined, instrumentType: null,
      symbolRoot: '   ', expiryFrom: '', expiryTo: undefined,
    });
    expect(out).toEqual({});
  });

  it('maps every filter to its backend query-param name', () => {
    const out = buildRealizedPnlParams({
      account: 'ZG0790', segment: 'derivatives', instrumentType: 'option',
      symbolRoot: 'goldm', expiryFrom: '2026-10-01', expiryTo: '2026-11-30',
    });
    expect(out).toEqual({
      account: 'ZG0790',
      segment: 'derivatives',
      instrument_type: 'option',
      symbol_root: 'GOLDM',
      expiry_from: '2026-10-01',
      expiry_to: '2026-11-30',
    });
  });

  it('trims whitespace from every value and uppercases symbol_root', () => {
    const out = buildRealizedPnlParams({ account: '  ZG0790  ', symbolRoot: '  nifty  ' });
    expect(out.account).toBe('ZG0790');
    expect(out.symbol_root).toBe('NIFTY');
  });

  it('includes only the filters that are set (partial filter set)', () => {
    const out = buildRealizedPnlParams({ segment: 'equity' });
    expect(out).toEqual({ segment: 'equity' });
  });
});

describe('normalizeRealizedPnlRows', () => {
  const ROW = { account: 'ZG0790', segment: 'derivatives', instrument_type: 'option', symbol_root: 'GOLDM', expiry: '2026-11-25', realized_pnl: 1234.5 };

  it('accepts a bare array (the agreed contract shape)', () => {
    expect(normalizeRealizedPnlRows([ROW])).toEqual([ROW]);
  });

  it('accepts an {rows:[...]} envelope (the shape every sibling /admin/history/* endpoint uses)', () => {
    expect(normalizeRealizedPnlRows({ rows: [ROW] })).toEqual([ROW]);
  });

  it('returns [] for null/undefined/malformed responses without throwing', () => {
    expect(normalizeRealizedPnlRows(null)).toEqual([]);
    expect(normalizeRealizedPnlRows(undefined)).toEqual([]);
    expect(normalizeRealizedPnlRows({})).toEqual([]);
    expect(normalizeRealizedPnlRows({ rows: null })).toEqual([]);
    expect(normalizeRealizedPnlRows('not an object')).toEqual([]);
    expect(normalizeRealizedPnlRows(42)).toEqual([]);
  });

  it('returns [] for an empty array/envelope', () => {
    expect(normalizeRealizedPnlRows([])).toEqual([]);
    expect(normalizeRealizedPnlRows({ rows: [] })).toEqual([]);
  });
});

describe('sumRealizedPnl', () => {
  it('returns 0 for null/undefined/empty input (no rows, no error — a real zero)', () => {
    expect(sumRealizedPnl(null)).toBe(0);
    expect(sumRealizedPnl(undefined)).toBe(0);
    expect(sumRealizedPnl([])).toBe(0);
  });

  it('sums realized_pnl across rows, including negative values', () => {
    const rows = [
      { realized_pnl: 1000 },
      { realized_pnl: -400 },
      { realized_pnl: 250.5 },
    ];
    expect(sumRealizedPnl(rows)).toBeCloseTo(850.5, 5);
  });

  it('skips null/undefined/NaN rows rather than poisoning the whole total', () => {
    const rows = [
      { realized_pnl: 1000 },
      { realized_pnl: null },
      { realized_pnl: undefined },
      { realized_pnl: 'not-a-number' },
      { realized_pnl: 500 },
    ];
    expect(sumRealizedPnl(rows)).toBe(1500);
  });

  it('skips a row missing the field entirely', () => {
    const rows = [{ realized_pnl: 100 }, { account: 'ZG0790' /* no realized_pnl key */ }];
    expect(sumRealizedPnl(rows)).toBe(100);
  });
});
