import { describe, it, expect } from 'vitest';
import { pairGroupSort, pairChipHtml } from '../../data/pairGroupSort.js';

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
// pairChipHtml — chip generation
// ---------------------------------------------------------------------------

describe('pairChipHtml — pair-chip span', () => {
  it('returns pair-chip span when pair_group_key is set', () => {
    const html = pairChipHtml({ pair_group_key: 'P1', orphan_qty: 0, paired_qty: 2 });
    expect(html).toContain('class="pair-chip"');
    expect(html).toContain('P1');
  });

  it('includes the pair_group_key text in the span', () => {
    const html = pairChipHtml({ pair_group_key: 'P3', orphan_qty: 0, paired_qty: 4 });
    expect(html).toContain('>P3<');
  });
});

describe('pairChipHtml — orphan-chip span', () => {
  it('returns both chips when orphan_qty > 0 and paired_qty > 0', () => {
    const html = pairChipHtml({ pair_group_key: 'P1', orphan_qty: 1, paired_qty: 2 });
    expect(html).toContain('class="pair-chip"');
    expect(html).toContain('class="orphan-chip"');
    expect(html).toContain('1L orphan');
  });

  it('does NOT return orphan chip when orphan_qty is 0', () => {
    const html = pairChipHtml({ pair_group_key: 'P1', orphan_qty: 0, paired_qty: 2 });
    expect(html).not.toContain('orphan-chip');
  });

  it('does NOT return orphan chip when paired_qty is 0', () => {
    const html = pairChipHtml({ pair_group_key: 'P1', orphan_qty: 2, paired_qty: 0 });
    expect(html).not.toContain('orphan-chip');
  });

  it('does NOT return orphan chip when both orphan_qty and paired_qty are 0', () => {
    const html = pairChipHtml({ pair_group_key: 'P1', orphan_qty: 0, paired_qty: 0 });
    expect(html).not.toContain('orphan-chip');
  });
});

describe('pairChipHtml — empty cases', () => {
  it('returns empty string when no pair_group_key and no orphan_qty', () => {
    const html = pairChipHtml({ pair_group_key: null, orphan_qty: 0, paired_qty: 0 });
    expect(html).toBe('');
  });

  it('returns empty string when pair_group_key is undefined', () => {
    const html = pairChipHtml({ orphan_qty: 0, paired_qty: 0 });
    expect(html).toBe('');
  });

  it('returns empty string when data is null', () => {
    expect(pairChipHtml(null)).toBe('');
  });

  it('returns empty string when data is undefined', () => {
    expect(pairChipHtml(undefined)).toBe('');
  });

  it('returns empty string when data is an empty object', () => {
    expect(pairChipHtml({})).toBe('');
  });
});
