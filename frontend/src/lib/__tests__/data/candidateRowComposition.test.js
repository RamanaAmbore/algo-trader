/**
 * candidateRowComposition.test.js — Vitest tests for the derivatives
 * Legs/Candidates grid's `displayedCandidates` composition (column-sort
 * plan, Section E), pulled out of +page.svelte so the REAL composition
 * code is exercised, not a hand-mirrored copy.
 *
 * Five quality dimensions:
 *  1. SSOT  — tests the real `composeDisplayedCandidates`, and the
 *             null-sort case against the real `sortRowsByPairGroup`
 *             (no re-derivation of its sort key rules).
 *  2. Perf  — asserts `applyColumnSort` is never invoked when
 *             direction == null (via a spy columnSpecs accessor).
 *  3. Stale — CRITICAL regression guard: CLAUDE.md documents two prior
 *             live incidents from corrupting pair-group block ordering
 *             when composed with a column/account sort. The null-sort
 *             fixture below deliberately orders two draft rows OUT of
 *             their post-pair-sort relative order in the raw input, so
 *             a "partition drafts first, keep their original order"
 *             regression (the literal first-draft plan's own proposed
 *             mechanism, rejected for this exact reason) would fail it.
 *  4. Reuse — exercises the real exported function only.
 *  5. UX    — the Exp Close tab is proven completely unaffected by any
 *             candSort state, matching the "header clicks stay inert on
 *             that tab" requirement.
 */

import { describe, it, expect } from 'vitest';
import { composeDisplayedCandidates } from '$lib/data/candidateRowComposition.js';
import { sortRowsByPairGroup } from '$lib/data/pairGroupSort.js';

function row(symbol, account, extra = {}) {
  return { symbol, account, source: 'live', ...extra };
}

/**
 * Typed helper so `svelte-check` doesn't widen `type: 'text'|'number'`
 * literals to a bare `string` when building a columnSpecs object inline
 * (same convention as columnSort.test.js's own `spec` helper).
 * @param {'text'|'number'} type
 * @param {(row: any) => any} accessor
 * @returns {import('$lib/data/columnSort.js').ColumnSpec}
 */
function spec(type, accessor) {
  return { type, accessor };
}

// Fixture: two accounts, a paired option leg per account, a lone future,
// an orphan option, and two drafts — one per account, placed in the RAW
// array in the OPPOSITE order from how they'll sort by account.
const FIXTURE = [
  row('DRAFT2', 'ZJ6294', { source: 'draft', draftId: 2 }),              // 0
  row('NIFTY26JUNFUT', 'ZG0790'),                                         // 1
  row('NIFTY26JUN22000CE', 'ZG0790', { pair_group_key: 'P1' }),          // 2
  row('DRAFT1', 'ZG0790', { source: 'draft', draftId: 1 }),              // 3
  row('BANKNIFTY26JUN45000PE', 'ZJ6294', { pair_group_key: 'P2' }),      // 4
  row('NIFTY26JUN22000CE', 'ZJ6294'),                                    // 5 orphan (no pair_group_key)
];

describe('composeDisplayedCandidates — null-sort path (CRITICAL byte-identical invariant)', () => {
  it('equals sortRowsByPairGroup(rows) followed by the drafts-last reshuffle — the literal pre-column-sort code', () => {
    const out = composeDisplayedCandidates(FIXTURE, {
      isExpiryTab: false,
      sortState: { column: null, direction: null },
      columnSpecs: {},
    });

    // Literal reference — the exact two lines +page.svelte ran before
    // the column-sort feature existed.
    const ref = sortRowsByPairGroup(FIXTURE);
    const refOrdered = ref.filter((c) => c.source !== 'draft').concat(ref.filter((c) => c.source === 'draft'));

    expect(out.map((r) => r.symbol + '|' + r.account)).toEqual(
      refOrdered.map((r) => r.symbol + '|' + r.account)
    );
  });

  it('drafts sort into their PAIR-GROUP order (by account), not their raw array order — proves sort-then-partition, not partition-then-sort', () => {
    const out = composeDisplayedCandidates(FIXTURE, {
      isExpiryTab: false,
      sortState: { column: null, direction: null },
      columnSpecs: {},
    });

    // Hand-computed expected order (see the file's own module docstring
    // for the full derivation): account ZG0790 sorts before ZJ6294, so
    // DRAFT1 (ZG0790) must come before DRAFT2 (ZJ6294) even though
    // DRAFT2 appears FIRST in the raw fixture array.
    expect(out.map((r) => r.symbol)).toEqual([
      'NIFTY26JUNFUT',
      'NIFTY26JUN22000CE', // ZG0790, P1
      'BANKNIFTY26JUN45000PE', // ZJ6294, P2
      'NIFTY26JUN22000CE', // ZJ6294, orphan
      'DRAFT1',
      'DRAFT2',
    ]);
  });

  it('never calls applyColumnSort (and thus never evaluates a columnSpecs accessor) when direction is null', () => {
    const throwingSpecs = {
      symbol: spec('text', () => { throw new Error('accessor must not be called when direction is null'); }),
    };
    expect(() => composeDisplayedCandidates(FIXTURE, {
      isExpiryTab: false,
      sortState: { column: 'symbol', direction: null },
      columnSpecs: throwingSpecs,
    })).not.toThrow();
  });
});

describe('composeDisplayedCandidates — active column sort', () => {
  const columnSpecs = { symbol: spec('text', (c) => c.symbol) };

  it('column-sorts the real rows, and drafts still render last unconditionally', () => {
    const out = composeDisplayedCandidates(FIXTURE, {
      isExpiryTab: false,
      sortState: { column: 'symbol', direction: 'asc' },
      columnSpecs,
    });

    const draftIdx = out.findIndex((r) => r.source === 'draft');
    const lastRealIdx = out.length - 1 - [...out].reverse().findIndex((r) => r.source === 'draft');
    // Every draft row is at or after the first draft index, and no real
    // row appears after any draft row.
    const realAfterDraft = out.slice(draftIdx).some((r) => r.source !== 'draft');
    expect(realAfterDraft).toBe(false);

    // Among the real rows, symbol order is ascending (ties broken stably).
    const realSymbols = out.filter((r) => r.source !== 'draft').map((r) => r.symbol);
    const sortedCopy = [...realSymbols].sort();
    expect(realSymbols).toEqual(sortedCopy);
  });

  it('descending sort reverses real-row order; drafts are still last', () => {
    const out = composeDisplayedCandidates(FIXTURE, {
      isExpiryTab: false,
      sortState: { column: 'symbol', direction: 'desc' },
      columnSpecs,
    });
    const reals = out.filter((r) => r.source !== 'draft').map((r) => r.symbol);
    expect(reals).toEqual([...reals].sort().reverse());
    expect(out.slice(-2).every((r) => r.source === 'draft')).toBe(true);
  });
});

describe('composeDisplayedCandidates — Exp Close tab is completely unaffected by candSort', () => {
  it('returns rows untouched by sort/draft logic regardless of sortState', () => {
    const expiryRows = [
      { symbol: 'A', _band: 'close' },
      { symbol: 'B', _band: 'otm' },
    ];
    const outNull = composeDisplayedCandidates(expiryRows, {
      isExpiryTab: true,
      sortState: { column: null, direction: null },
      columnSpecs: {},
    });
    const outActive = composeDisplayedCandidates(expiryRows, {
      isExpiryTab: true,
      sortState: { column: 'symbol', direction: 'desc' },
      columnSpecs: { symbol: spec('text', (c) => c.symbol) },
    });
    expect(outNull.map((r) => r.symbol)).toEqual(['A', 'B']);
    expect(outActive.map((r) => r.symbol)).toEqual(['A', 'B']);
  });

  it('still stamps _ord (shared {#each} key fallback needs it on both tabs)', () => {
    const expiryRows = [{ symbol: 'A' }, { symbol: 'B' }];
    const out = composeDisplayedCandidates(expiryRows, {
      isExpiryTab: true,
      sortState: { column: null, direction: null },
      columnSpecs: {},
    });
    expect(out.map((r) => r._ord)).toEqual([0, 1]);
  });
});
