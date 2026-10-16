import { describe, it, expect } from 'vitest';
import { pairGroupSort, sortRowsByPairGroup } from '../../data/pairGroupSort.js';
import { positionStateBadge } from '../../data/positionStateBadge.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Build a minimal IRowNode-like object for pairGroupSort tests.
 * @param {string} symbol
 * @param {string|null} [pair_group_key]
 */
function makeNode(symbol, pair_group_key = null) {
  return { data: { tradingsymbol: symbol, pair_group_key } };
}

// ---------------------------------------------------------------------------
// pairGroupSort — primary group ordering
// ---------------------------------------------------------------------------

describe('pairGroupSort — group ordering', () => {
  it('places P1 rows before P2 rows before orphan rows', () => {
    const orphan = makeNode('NIFTY25JUN24000PE', null);
    const p2     = makeNode('NIFTY25JUN25000CE', 'P2');
    const p1     = makeNode('NIFTY25JUN24500CE', 'P1');

    const nodes = [orphan, p2, p1];
    pairGroupSort(nodes);

    expect(nodes[0]).toBe(p1);    // P1 first
    expect(nodes[1]).toBe(p2);    // P2 second
    expect(nodes[2]).toBe(orphan); // orphan (no key) last
  });

  it('places all keyed groups before rows without a key', () => {
    const a = makeNode('SYMA', 'P1');
    const b = makeNode('SYMB', null);
    const c = makeNode('SYMC', null);
    const d = makeNode('SYMD', 'P2');

    const nodes = [b, a, c, d];
    pairGroupSort(nodes);

    // Keyed rows (P1 then P2) come before null rows
    expect(nodes[0]).toBe(a);
    expect(nodes[1]).toBe(d);
    // The two null rows come last
    expect(nodes.slice(2).map(n => n.data.tradingsymbol)).toEqual(
      expect.arrayContaining(['SYMB', 'SYMC'])
    );
  });

  it('handles all rows in the same group — preserves order', () => {
    const a = makeNode('SYMA', 'P1');
    const b = makeNode('SYMB', 'P1');
    const c = makeNode('SYMC', 'P1');

    const nodes = [a, b, c];
    pairGroupSort(nodes);

    expect(nodes[0]).toBe(a);
    expect(nodes[1]).toBe(b);
    expect(nodes[2]).toBe(c);
  });

  it('handles all orphan rows (no keys) — order unchanged', () => {
    const a = makeNode('SYMA', null);
    const b = makeNode('SYMB', null);
    const nodes = [a, b];
    pairGroupSort(nodes);

    expect(nodes[0]).toBe(a);
    expect(nodes[1]).toBe(b);
  });

  it('returns early on empty array without throwing', () => {
    const nodes = [];
    expect(() => pairGroupSort(nodes)).not.toThrow();
    expect(nodes).toHaveLength(0);
  });

  it('handles null/undefined rowNodes without throwing', () => {
    expect(() => pairGroupSort(null)).not.toThrow();
    expect(() => pairGroupSort(undefined)).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// pairGroupSort — stable within-group ordering
// ---------------------------------------------------------------------------

describe('pairGroupSort — stable within-group ordering', () => {
  it('preserves original index order within a group', () => {
    // P1 group: three rows in a specific original order
    const first  = makeNode('FIRST',  'P1');
    const second = makeNode('SECOND', 'P1');
    const third  = makeNode('THIRD',  'P1');
    const orphan = makeNode('ORPH',   null);

    // Input: orphan first so we can see that group-internal order is preserved
    const nodes = [orphan, first, second, third];
    pairGroupSort(nodes);

    // All P1 rows come first; within P1 the original order is preserved
    expect(nodes[0]).toBe(first);
    expect(nodes[1]).toBe(second);
    expect(nodes[2]).toBe(third);
    expect(nodes[3]).toBe(orphan);
  });

  it('preserves order within P2 group when mixed with P1', () => {
    const p1a = makeNode('P1A', 'P1');
    const p2a = makeNode('P2A', 'P2');
    const p1b = makeNode('P1B', 'P1');
    const p2b = makeNode('P2B', 'P2');

    // Interleaved input: P1A, P2A, P1B, P2B
    const nodes = [p1a, p2a, p1b, p2b];
    pairGroupSort(nodes);

    // P1 cluster first: P1A before P1B (original relative order)
    expect(nodes[0]).toBe(p1a);
    expect(nodes[1]).toBe(p1b);
    // P2 cluster after: P2A before P2B
    expect(nodes[2]).toBe(p2a);
    expect(nodes[3]).toBe(p2b);
  });
});

// ---------------------------------------------------------------------------
// pos_state cell logic — this file used to carry its own local mirrors of
// the cellStyle/cellRenderer logic used by pulseColumns.js/PerformancePage
// (`posStateCellStyle`/`posStateCellRenderer`, now removed). Both real
// sites now delegate to the shared `positionStateBadge.js` helper
// (2026-10 fix) — see positionStateBadge.test.js for the full priority-
// order + glyph test suite. These tests just confirm THIS file's own
// pairGroupSort-adjacent expectations (GTT > pinned > paired > orphan,
// stale "○"/hex literals retired) still hold against the real helper.
// ---------------------------------------------------------------------------

describe('positionStateBadge — priority order (via the real shared helper)', () => {
  it('returns gtt state when has_gtt is truthy', () => {
    const b = positionStateBadge({ has_gtt: true, pair_group_key: 'P1', is_orphan: false });
    expect(b.state).toBe('gtt');
    expect(b.color).toBe('var(--algo-green)');
  });

  it('returns paired state when pair_group_key is set and has_gtt is false', () => {
    const b = positionStateBadge({ has_gtt: false, pair_group_key: 'P1', is_orphan: false });
    expect(b.state).toBe('paired');
    expect(b.color).toBe('var(--algo-cyan-text)');
  });

  it('returns orphan state when is_orphan is true and no key/gtt', () => {
    const b = positionStateBadge({ has_gtt: false, pair_group_key: null, is_orphan: true });
    expect(b.state).toBe('orphan');
    expect(b.color).toBe('var(--algo-amber)');
  });

  it('returns null state when no state flags are set', () => {
    const b = positionStateBadge({ has_gtt: false, pair_group_key: null, is_orphan: false });
    expect(b.state).toBeNull();
  });

  it('returns null state for _isTotal rows', () => {
    const b = positionStateBadge({ _isTotal: true, has_gtt: true, pair_group_key: 'P1' });
    expect(b.state).toBeNull();
  });

  it('returns null state for null/undefined data', () => {
    expect(positionStateBadge(null).state).toBeNull();
    expect(positionStateBadge(undefined).state).toBeNull();
  });

  it('GTT takes priority over pair_group_key (both set)', () => {
    const b = positionStateBadge({ has_gtt: true, pair_group_key: 'P1', is_orphan: true });
    expect(b.state).toBe('gtt');
  });

  it('pinned takes priority over auto-paired (both keys set)', () => {
    const b = positionStateBadge({ pin_group_key: 'PIN1', pair_group_key: 'P1' });
    expect(b.state).toBe('pinned');
  });
});

// ---------------------------------------------------------------------------
// sortRowsByPairGroup — pure-array variant for the derivatives Legs grid
// (hand-rolled {#each}, not ag-Grid IRowNode[]). Restores the grouping
// that was removed as dead code in commit 56712d19 (the data never
// reached this page's pipeline before the 2026-10 whitelist fix).
// ---------------------------------------------------------------------------

describe('sortRowsByPairGroup — array variant, group ordering', () => {
  function row(symbol, pair_group_key = null, account = 'ACC1') {
    return { symbol, pair_group_key, account };
  }

  it('places P1 rows before P2 rows before orphan (no key) rows', () => {
    const orphan = row('NIFTY25JUN24000PE', null);
    const p2     = row('NIFTY25JUN25000CE', 'P2');
    const p1     = row('NIFTY25JUN24500CE', 'P1');

    const sorted = sortRowsByPairGroup([orphan, p2, p1]);

    expect(sorted[0]).toBe(p1);
    expect(sorted[1]).toBe(p2);
    expect(sorted[2]).toBe(orphan);
  });

  it('preserves original index order within a group (stable sort)', () => {
    const first  = row('FIRST',  'P1');
    const second = row('SECOND', 'P1');
    const third  = row('THIRD',  'P1');
    const orphan = row('ORPH',   null);

    const sorted = sortRowsByPairGroup([orphan, first, second, third]);

    expect(sorted[0]).toBe(first);
    expect(sorted[1]).toBe(second);
    expect(sorted[2]).toBe(third);
    expect(sorted[3]).toBe(orphan);
  });

  it('does not mutate the input array', () => {
    const input = [row('A', null), row('B', 'P1')];
    const inputCopy = [...input];
    sortRowsByPairGroup(input);
    expect(input).toEqual(inputCopy);
  });

  it('returns [] for empty/null/undefined input without throwing', () => {
    expect(sortRowsByPairGroup([])).toEqual([]);
    expect(sortRowsByPairGroup(null)).toEqual([]);
    expect(sortRowsByPairGroup(undefined)).toEqual([]);
  });

  it('keys on account+pair_group_key, so two accounts\' unrelated "P1" groups do not interleave', () => {
    const acc1p1a = row('ACC1_LEG_A', 'P1', 'ACC1');
    const acc1p1b = row('ACC1_LEG_B', 'P1', 'ACC1');
    const acc2p1a = row('ACC2_LEG_A', 'P1', 'ACC2');
    const acc2p1b = row('ACC2_LEG_B', 'P1', 'ACC2');

    // Interleaved input across accounts.
    const sorted = sortRowsByPairGroup([acc1p1a, acc2p1a, acc1p1b, acc2p1b]);

    // ACC1's P1 pair stays adjacent; ACC2's P1 pair stays adjacent — the
    // two groups are NOT merged into one four-row block.
    const acc1Idx = [sorted.indexOf(acc1p1a), sorted.indexOf(acc1p1b)].sort();
    const acc2Idx = [sorted.indexOf(acc2p1a), sorted.indexOf(acc2p1b)].sort();
    expect(acc1Idx[1] - acc1Idx[0]).toBe(1); // adjacent
    expect(acc2Idx[1] - acc2Idx[0]).toBe(1); // adjacent
  });
});
