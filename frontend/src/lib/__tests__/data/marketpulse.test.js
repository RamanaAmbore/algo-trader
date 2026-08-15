import { describe, it, expect } from 'vitest';

// ---------------------------------------------------------------------------
// Helpers — self-contained equivalents of the component-internal functions
// so we can test the logic without importing Svelte.
// ---------------------------------------------------------------------------

/**
 * Minimal equivalent of the `badge-o` branch in MarketPulse._symCellBadges().
 * Only tests the orphan chip logic (Change A).
 * @param {Record<string, any>} row
 * @returns {string[]} list of class names that would appear in the badges
 */
function badgeClassesForRow(row) {
  const classes = [];
  if (row.src?.p && row.is_orphan) {
    classes.push('badge-o');
  }
  return classes;
}

/**
 * Pair-group post-sort — exact algorithm from Change H (_pairGroupPostSort).
 * Kept in the test file so the test is self-contained and doesn't depend on
 * the component closure.
 * @param {{ nodes: Array<{ data?: { pair_group_key?: string } }> }} params
 */
function pairGroupPostSort(params) {
  const nodes = params.nodes;
  const groups = new Map();
  for (const n of nodes) {
    const k = n.data?.pair_group_key;
    if (k) {
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(n);
    }
  }
  const order = [];
  const seen  = new Set();
  for (const n of nodes) {
    if (seen.has(n)) continue;
    seen.add(n);
    order.push(n);
    const k = n.data?.pair_group_key;
    if (k) {
      for (const m of groups.get(k) ?? []) {
        if (!seen.has(m)) { seen.add(m); order.push(m); }
      }
    }
  }
  nodes.length = 0;
  nodes.push(...order);
}

/** Build a minimal IRowNode-like object for pairGroupPostSort tests. */
function makeNode(id, pair_group_key = undefined) {
  return { _id: id, data: pair_group_key != null ? { pair_group_key } : {} };
}

// ---------------------------------------------------------------------------
// Change A — badge-o chip in _symCellBadges
// ---------------------------------------------------------------------------

describe('badge-o chip logic (Change A)', () => {
  it('pushes badge-o when src.p=true and is_orphan=true', () => {
    const row = { src: { p: true }, is_orphan: true };
    expect(badgeClassesForRow(row)).toContain('badge-o');
  });

  it('does NOT push badge-o when is_orphan=false', () => {
    const row = { src: { p: true }, is_orphan: false };
    expect(badgeClassesForRow(row)).not.toContain('badge-o');
  });

  it('does NOT push badge-o when src.p is absent', () => {
    const row = { src: { h: true }, is_orphan: true };
    expect(badgeClassesForRow(row)).not.toContain('badge-o');
  });

  it('does NOT push badge-o when src is absent', () => {
    const row = { is_orphan: true };
    expect(badgeClassesForRow(row)).not.toContain('badge-o');
  });

  it('does NOT push badge-o when both flags are absent', () => {
    const row = {};
    expect(badgeClassesForRow(row)).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Change H — postSortRows pair-group clustering
// ---------------------------------------------------------------------------

describe('pairGroupPostSort (Change H)', () => {
  it('keeps child node immediately after parent when pair_group_key matches', () => {
    // A(g1), B(no key), C(g1) → expected: [A, C, B]
    const A = makeNode('A', 'g1');
    const B = makeNode('B');
    const C = makeNode('C', 'g1');
    const params = { nodes: [A, B, C] };
    pairGroupPostSort(params);
    expect(params.nodes.map(n => n._id)).toEqual(['A', 'C', 'B']);
  });

  it('preserves order for rows with no pair_group_key', () => {
    const X = makeNode('X');
    const Y = makeNode('Y');
    const params = { nodes: [X, Y] };
    pairGroupPostSort(params);
    expect(params.nodes.map(n => n._id)).toEqual(['X', 'Y']);
  });

  it('clusters multiple groups independently', () => {
    // A(g1), B(g2), C(g1), D(g2) → [A, C, B, D]
    const A = makeNode('A', 'g1');
    const B = makeNode('B', 'g2');
    const C = makeNode('C', 'g1');
    const D = makeNode('D', 'g2');
    const params = { nodes: [A, B, C, D] };
    pairGroupPostSort(params);
    expect(params.nodes.map(n => n._id)).toEqual(['A', 'C', 'B', 'D']);
  });

  it('handles single-node group (no-op)', () => {
    const A = makeNode('A', 'g1');
    const B = makeNode('B');
    const params = { nodes: [A, B] };
    pairGroupPostSort(params);
    expect(params.nodes.map(n => n._id)).toEqual(['A', 'B']);
  });

  it('handles empty node list', () => {
    const params = { nodes: [] };
    pairGroupPostSort(params);
    expect(params.nodes).toHaveLength(0);
  });
});
