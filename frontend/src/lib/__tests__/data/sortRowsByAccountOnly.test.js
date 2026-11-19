/**
 * sortRowsByAccountOnly.test.js — Vitest tests for the derivatives Legs
 * grid's new unpaired "Legs" view sort (2026-11, Legs/Paired/Exp Close
 * dropdown). Exercises the real exported `sortRowsByAccountOnly` from
 * `pairGroupSort.js` directly (no hand-mirrored copy).
 *
 * Five quality dimensions:
 *  1. SSOT   — tests the real exported function only.
 *  2. Perf   — n/a (pure array sort, no I/O); covered implicitly by the
 *              function's O(n log n) sort with no repeated re-parsing.
 *  3. Stale  — regression guard: this view must NEVER cluster by
 *              `pair_group_key` (that's `sortRowsByPairGroup`'s job);
 *              a fixture with pair_group_key set is included to prove
 *              it is ignored.
 *  4. Reuse  — exercises `decomposeSymbol`-derived root/tier/strike/
 *              optType, the same parser the rest of the app uses.
 *  5. UX     — asserts the operator-visible ordering: account grouping,
 *              futures above their own root's options, strikes
 *              ascending, CE before PE at the same strike.
 */

import { describe, it, expect } from 'vitest';
import { sortRowsByAccountOnly } from '$lib/data/pairGroupSort.js';

function row(symbol, account, extra = {}) {
  return { symbol, account, ...extra };
}

describe('sortRowsByAccountOnly — account grouping', () => {
  it('groups rows from multiple accounts together, in lexicographic account order, regardless of raw input order', () => {
    // Interleaved input: ZJ6294 rows appear BEFORE ZG0790 rows, and the
    // two accounts' rows alternate — the sort must still cluster every
    // ZG0790 row before every ZJ6294 row.
    const rows = [
      row('NIFTY26JUN22000CE', 'ZJ6294'),
      row('BANKNIFTY26JUN45000PE', 'ZG0790'),
      row('NIFTY26JUN23000PE', 'ZJ6294'),
      row('RELIANCE', 'ZG0790'),
    ];
    const out = sortRowsByAccountOnly(rows);
    const accounts = out.map((r) => r.account);
    const firstZJIdx = accounts.indexOf('ZJ6294');
    const lastZGIdx = accounts.lastIndexOf('ZG0790');
    expect(lastZGIdx).toBeLessThan(firstZJIdx);
    expect(accounts).toEqual(['ZG0790', 'ZG0790', 'ZJ6294', 'ZJ6294']);
  });

  it('never clusters by pair_group_key — a P2 row in account A sorts by its own root/strike, not after a P1 row in a different account', () => {
    const rows = [
      row('NIFTY26JUN25000CE', 'ACC2', { pair_group_key: 'P1' }),
      row('NIFTY26JUN20000CE', 'ACC1', { pair_group_key: 'P2' }),
    ];
    const out = sortRowsByAccountOnly(rows);
    // Account is still the primary key: ACC1 before ACC2, even though
    // ACC1's row carries the "later" pair_group_key P2.
    expect(out.map((r) => r.symbol)).toEqual([
      'NIFTY26JUN20000CE', // ACC1
      'NIFTY26JUN25000CE', // ACC2
    ]);
  });
});

describe('sortRowsByAccountOnly — within-account ordering: root, tier, strike, CE/PE', () => {
  it('strikes sort ascending within the same root/account', () => {
    const rows = [
      row('NIFTY26JUN25000CE', 'ZG0790'),
      row('NIFTY26JUN20000CE', 'ZG0790'),
      row('NIFTY26JUN22500CE', 'ZG0790'),
    ];
    const out = sortRowsByAccountOnly(rows);
    expect(out.map((r) => r.symbol)).toEqual([
      'NIFTY26JUN20000CE',
      'NIFTY26JUN22500CE',
      'NIFTY26JUN25000CE',
    ]);
  });

  it('CE sorts before PE at the same strike', () => {
    const rows = [
      row('NIFTY26JUN22000PE', 'ZG0790'),
      row('NIFTY26JUN22000CE', 'ZG0790'),
    ];
    const out = sortRowsByAccountOnly(rows);
    expect(out.map((r) => r.symbol)).toEqual([
      'NIFTY26JUN22000CE',
      'NIFTY26JUN22000PE',
    ]);
  });

  it('a future sorts above its own root\'s options within the same account (tier: underlying/future before option)', () => {
    const rows = [
      row('NIFTY26JUN22000PE', 'ZG0790'),
      row('NIFTY26JUN22000CE', 'ZG0790'),
      row('NIFTY26JUNFUT', 'ZG0790'),
    ];
    const out = sortRowsByAccountOnly(rows);
    expect(out.map((r) => r.symbol)).toEqual([
      'NIFTY26JUNFUT',
      'NIFTY26JUN22000CE',
      'NIFTY26JUN22000PE',
    ]);
  });

  it('a bare equity/holding row sorts by its own root text, mixed in with F&O roots on the same account', () => {
    const rows = [
      row('NIFTY26JUNFUT', 'ZG0790'),
      row('AXISBANK', 'ZG0790'),
    ];
    const out = sortRowsByAccountOnly(rows);
    // "AXISBANK" < "NIFTY26JUNFUT"'s root "NIFTY" alphabetically.
    expect(out.map((r) => r.symbol)).toEqual([
      'AXISBANK',
      'NIFTY26JUNFUT',
    ]);
  });

  it('full combined ordering: two accounts, each with a future + CE/PE pair at multiple strikes, interleaved input', () => {
    const rows = [
      row('NIFTY26JUN22000PE', 'ZJ6294'),
      row('BANKNIFTY26JUNFUT', 'ZG0790'),
      row('NIFTY26JUNFUT', 'ZJ6294'),
      row('BANKNIFTY26JUN45000CE', 'ZG0790'),
      row('NIFTY26JUN22000CE', 'ZJ6294'),
      row('BANKNIFTY26JUN44000PE', 'ZG0790'),
    ];
    const out = sortRowsByAccountOnly(rows);
    expect(out.map((r) => r.symbol + '|' + r.account)).toEqual([
      'BANKNIFTY26JUNFUT|ZG0790',
      'BANKNIFTY26JUN44000PE|ZG0790',
      'BANKNIFTY26JUN45000CE|ZG0790',
      'NIFTY26JUNFUT|ZJ6294',
      'NIFTY26JUN22000CE|ZJ6294',
      'NIFTY26JUN22000PE|ZJ6294',
    ]);
  });
});

describe('sortRowsByAccountOnly — edge cases', () => {
  it('returns an empty array for null/undefined/empty input without throwing', () => {
    expect(sortRowsByAccountOnly(null)).toEqual([]);
    expect(sortRowsByAccountOnly(undefined)).toEqual([]);
    expect(sortRowsByAccountOnly([])).toEqual([]);
  });

  it('does not mutate the input array', () => {
    const rows = [row('B', 'ACC2'), row('A', 'ACC1')];
    const copy = [...rows];
    sortRowsByAccountOnly(rows);
    expect(rows).toEqual(copy);
  });

  it('is stable: two rows with an identical sort key keep their original relative order', () => {
    const rows = [
      row('NIFTY26JUN22000CE', 'ZG0790', { extra: 'first' }),
      row('NIFTY26JUN22000CE', 'ZG0790', { extra: 'second' }),
    ];
    const out = sortRowsByAccountOnly(rows);
    expect(out.map((r) => r.extra)).toEqual(['first', 'second']);
  });
});
