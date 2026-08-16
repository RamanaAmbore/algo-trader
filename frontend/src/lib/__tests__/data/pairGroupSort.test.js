import { describe, it, expect } from 'vitest';
import { pairGroupSort } from '../../data/pairGroupSort.js';

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
// pos_state cell logic — mirrors the cellStyle / cellRenderer used in
// pulseColumns.js mkRightColDefs and PerformancePage positionsCols.
// Extracted here as pure functions so they can be unit-tested without
// importing ag-Grid or mounting a Svelte component.
// ---------------------------------------------------------------------------

/**
 * Mirror of the pos_state cellStyle function used in pulseColumns.js and
 * PerformancePage.svelte.
 * @param {any} data  — row data object
 * @returns {{ background?: string, color?: string }}
 */
function posStateCellStyle(data) {
  if (!data || data._isTotal) return {};
  if (data.has_gtt)        return { background: 'rgba(74,222,128,0.20)',  color: '#4ade80' };
  if (data.pair_group_key) return { background: 'rgba(34,211,238,0.18)', color: '#67e8f9' };
  if (data.is_orphan)      return { background: 'rgba(251,191,36,0.15)', color: '#fbbf24' };
  return {};
}

/**
 * Mirror of the pos_state cellRenderer function used in pulseColumns.js and
 * PerformancePage.svelte.
 * @param {any} data  — row data object
 * @returns {string}
 */
function posStateCellRenderer(data) {
  if (!data || data._isTotal) return '';
  if (data.has_gtt)        return 'GTT';
  if (data.pair_group_key) return data.pair_group_key;
  if (data.is_orphan)      return '○';
  return '';
}

describe('pos_state — cellStyle priority order', () => {
  it('returns green GTT style when has_gtt is truthy', () => {
    const style = posStateCellStyle({ has_gtt: true, pair_group_key: 'P1', is_orphan: false });
    expect(style.color).toBe('#4ade80');
    expect(style.background).toContain('74,222,128');
  });

  it('returns cyan paired style when pair_group_key is set and has_gtt is false', () => {
    const style = posStateCellStyle({ has_gtt: false, pair_group_key: 'P1', is_orphan: false });
    expect(style.color).toBe('#67e8f9');
    expect(style.background).toContain('34,211,238');
  });

  it('returns amber orphan style when is_orphan is true and no key/gtt', () => {
    const style = posStateCellStyle({ has_gtt: false, pair_group_key: null, is_orphan: true });
    expect(style.color).toBe('#fbbf24');
    expect(style.background).toContain('251,191,36');
  });

  it('returns empty style when no state flags are set', () => {
    const style = posStateCellStyle({ has_gtt: false, pair_group_key: null, is_orphan: false });
    expect(style).toEqual({});
  });

  it('returns empty style for _isTotal rows', () => {
    const style = posStateCellStyle({ _isTotal: true, has_gtt: true, pair_group_key: 'P1' });
    expect(style).toEqual({});
  });

  it('returns empty style for null data', () => {
    expect(posStateCellStyle(null)).toEqual({});
    expect(posStateCellStyle(undefined)).toEqual({});
  });

  it('GTT takes priority over pair_group_key (both set)', () => {
    // has_gtt wins in the priority chain
    const style = posStateCellStyle({ has_gtt: true, pair_group_key: 'P1', is_orphan: true });
    expect(style.color).toBe('#4ade80');
  });
});

describe('pos_state — cellRenderer text', () => {
  it('returns "GTT" for has_gtt rows', () => {
    expect(posStateCellRenderer({ has_gtt: true })).toBe('GTT');
  });

  it('returns pair_group_key text for paired rows', () => {
    expect(posStateCellRenderer({ has_gtt: false, pair_group_key: 'P2' })).toBe('P2');
  });

  it('returns ○ for orphan rows', () => {
    expect(posStateCellRenderer({ has_gtt: false, pair_group_key: null, is_orphan: true })).toBe('○');
  });

  it('returns empty string for plain rows with no state', () => {
    expect(posStateCellRenderer({ has_gtt: false, pair_group_key: null, is_orphan: false })).toBe('');
  });

  it('returns empty string for _isTotal rows', () => {
    expect(posStateCellRenderer({ _isTotal: true, has_gtt: true, pair_group_key: 'P1' })).toBe('');
  });

  it('returns empty string for null/undefined data', () => {
    expect(posStateCellRenderer(null)).toBe('');
    expect(posStateCellRenderer(undefined)).toBe('');
  });
});
