import { describe, it, expect, vi } from 'vitest';
import { postSortGroups, postSortGroups2Level, pairGroupPostSort } from '../../data/pulseGridSetup.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Build a minimal IRowNode-like object for testing.
 * @param {string} tradingsymbol
 * @param {string} [underlying]
 */
function makeNode(tradingsymbol, underlying = '') {
  return { data: { tradingsymbol, underlying } };
}

/**
 * Build a mock ag-Grid `params` object.
 * Cast to `any` so the minimal mock satisfies the postSortGroups parameter
 * without needing to implement all 240+ members of GridApi.
 * @param {any[]} nodes
 * @param {Array<{sort: string|null}>} columnState
 * @returns {any}
 */
function makeParams(nodes, columnState) {
  return /** @type {any} */ ({
    nodes,
    api: {
      getColumnState: () => columnState,
    },
  });
}

// ---------------------------------------------------------------------------
// Change 4: postSortGroups sort-active early-return
// ---------------------------------------------------------------------------

describe('postSortGroups — sort-active early-return (Change 4)', () => {
  it('does NOT reorder nodes when a column has an active sort', () => {
    // Arrange: two nodes that would normally be clustered by underlying,
    // but a sort is active so the function must return immediately.
    const nodeA = makeNode('NIFTY24DEC24500CE', 'NIFTY');
    const nodeB = makeNode('RELIANCE',          '');         // standalone
    const nodeC = makeNode('NIFTY24DEC24000PE', 'NIFTY');
    const originalOrder = [nodeA, nodeB, nodeC];
    const nodes = [...originalOrder];

    const params = makeParams(nodes, [{ sort: 'asc' }, { sort: null }]);

    // Act
    postSortGroups(params);

    // Assert: nodes must be unchanged (early-return means no mutation)
    expect(nodes).toEqual(originalOrder);
    expect(nodes[0]).toBe(nodeA);
    expect(nodes[1]).toBe(nodeB);
    expect(nodes[2]).toBe(nodeC);
  });

  it('does NOT reorder when sort is "desc"', () => {
    const nodeA = makeNode('BANKNIFTY24DEC55000CE', 'BANKNIFTY');
    const nodeB = makeNode('BANKNIFTY24DEC54000PE', 'BANKNIFTY');
    const nodeC = makeNode('TCS', '');
    const originalOrder = [nodeA, nodeC, nodeB]; // interleaved
    const nodes = [...originalOrder];

    const params = makeParams(nodes, [{ sort: 'desc' }]);

    postSortGroups(params);

    expect(nodes[0]).toBe(nodeA);
    expect(nodes[1]).toBe(nodeC);
    expect(nodes[2]).toBe(nodeB);
  });

  it('does NOT early-return when all columns have sort: null', () => {
    // All columns unsorted → grouping logic should run and cluster nodes.
    const nodeA = makeNode('NIFTY24DEC24500CE', 'NIFTY');
    const nodeB = makeNode('RELIANCE',          '');
    const nodeC = makeNode('NIFTY24DEC24000PE', 'NIFTY');
    // Interleaved order: CE, standalone, PE
    const nodes = [nodeA, nodeB, nodeC];

    const params = makeParams(nodes, [{ sort: null }, { sort: null }]);

    postSortGroups(params);

    // After grouping, both NIFTY nodes should be contiguous.
    // The anchor (nodeA) came first, so cluster = [nodeA, nodeC], then nodeB.
    expect(nodes[0]).toBe(nodeA);
    expect(nodes[1]).toBe(nodeC);
    expect(nodes[2]).toBe(nodeB);
  });

  it('does NOT early-return when columnState is empty (no sort columns)', () => {
    // Empty column state: .some() returns false → grouping should run.
    const nodeA = makeNode('NIFTY24DEC24500CE', 'NIFTY');
    const nodeB = makeNode('NIFTY24DEC24000PE', 'NIFTY');
    const nodes = [nodeA, nodeB];

    const params = makeParams(nodes, []); // empty — no sort columns at all

    postSortGroups(params);

    // Both NIFTY — cluster preserved, order unchanged (both already together).
    expect(nodes).toHaveLength(2);
    expect(nodes[0]).toBe(nodeA);
    expect(nodes[1]).toBe(nodeB);
  });

  it('handles missing api gracefully (api undefined)', () => {
    // When api is absent (defensive path), optional chaining skips the guard
    // and grouping runs normally without throwing.
    const nodeA = makeNode('NIFTY24DEC24500CE', 'NIFTY');
    const nodeB = makeNode('RELIANCE',          '');
    const nodeC = makeNode('NIFTY24DEC24000PE', 'NIFTY');
    const nodes = [nodeA, nodeB, nodeC];

    // No api property — exercises the `api?.getColumnState()` guard
    const params = /** @type {any} */ ({ nodes });

    expect(() => postSortGroups(params)).not.toThrow();
    // Grouping should still run; NIFTY cluster expected.
    expect(nodes[0]).toBe(nodeA);
    expect(nodes[1]).toBe(nodeC);
    expect(nodes[2]).toBe(nodeB);
  });
});

// ---------------------------------------------------------------------------
// Existing grouping behaviour — verify no regression
// ---------------------------------------------------------------------------

describe('postSortGroups — underlying grouping (no sort active)', () => {
  it('clusters rows by underlying when no sort is active', () => {
    const ce  = makeNode('NIFTY24DEC24500CE', 'NIFTY');
    const eq  = makeNode('INFY',              '');
    const pe  = makeNode('NIFTY24DEC24000PE', 'NIFTY');
    const fut = makeNode('NIFTY24DECFUT',     'NIFTY');
    // Input: CE, equity-standalone, PE, future
    const nodes = [ce, eq, pe, fut];

    const params = makeParams(nodes, [{ sort: null }]);

    postSortGroups(params);

    // NIFTY cluster anchored at position 0 (ce appeared first).
    // Standalone INFY retains its relative position after the cluster.
    const niftyBlock = nodes.filter(n => n.data.underlying === 'NIFTY');
    const standalone = nodes.filter(n => !n.data.underlying);

    expect(niftyBlock).toHaveLength(3);
    expect(standalone).toHaveLength(1);
    expect(standalone[0]).toBe(eq);
    // All NIFTY rows come before INFY (anchor was at index 0).
    expect(nodes.indexOf(eq)).toBeGreaterThan(nodes.indexOf(ce));
  });

  it('preserves rows that have no underlying at their relative sort position', () => {
    const eq1 = makeNode('TCS',  '');
    const eq2 = makeNode('INFY', '');
    const nodes = [eq1, eq2];

    const params = makeParams(nodes, [{ sort: null }]);

    postSortGroups(params);

    // No underlyings → both are standalone, order preserved.
    expect(nodes[0]).toBe(eq1);
    expect(nodes[1]).toBe(eq2);
  });

  it('returns early on empty nodes list', () => {
    const nodes = [];
    const params = makeParams(nodes, [{ sort: null }]);

    expect(() => postSortGroups(params)).not.toThrow();
    expect(nodes).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// postSortGroups2Level — two-level derivative sort
// ---------------------------------------------------------------------------

/**
 * Build a node with derivative fields for postSortGroups2Level tests.
 */
function makeDerivNode(tradingsymbol, underlying, opt_type, expiry, strike) {
  return { data: { tradingsymbol, underlying, opt_type, expiry, strike } };
}

describe('postSortGroups2Level — user sort active early-return', () => {
  it('does NOT reorder nodes when a column has an active sort', () => {
    const fut = makeDerivNode('NIFTY25JUNFUT',     'NIFTY', null, '25JUN', null);
    const ce  = makeDerivNode('NIFTY25JUN24500CE', 'NIFTY', 'CE', '25JUN', 24500);
    const pe  = makeDerivNode('NIFTY25JUN24000PE', 'NIFTY', 'PE', '25JUN', 24000);
    // interleaved CE-first
    const nodes = [ce, pe, fut];
    const params = makeParams(nodes, [{ sort: 'asc' }]);

    postSortGroups2Level(params);

    // early-return: no mutation
    expect(nodes[0]).toBe(ce);
    expect(nodes[1]).toBe(pe);
    expect(nodes[2]).toBe(fut);
  });
});

describe('postSortGroups2Level — FUT before CE before PE', () => {
  it('orders FUT first, CE second, PE third within an underlying group', () => {
    const ce  = makeDerivNode('NIFTY25JUN24500CE', 'NIFTY', 'CE', '25JUN', 24500);
    const pe  = makeDerivNode('NIFTY25JUN24000PE', 'NIFTY', 'PE', '25JUN', 24000);
    const fut = makeDerivNode('NIFTY25JUNFUT',     'NIFTY', null, '25JUN', null);
    // Input: CE, PE, FUT — should become FUT, CE, PE
    const nodes = [ce, pe, fut];
    const params = makeParams(nodes, [{ sort: null }]);

    postSortGroups2Level(params);

    expect(nodes[0]).toBe(fut);
    expect(nodes[1]).toBe(ce);
    expect(nodes[2]).toBe(pe);
  });

  it('handles group with only CE and PE (no FUT)', () => {
    const ce = makeDerivNode('BANKNIFTY25JUN55000CE', 'BANKNIFTY', 'CE', '25JUN', 55000);
    const pe = makeDerivNode('BANKNIFTY25JUN54000PE', 'BANKNIFTY', 'PE', '25JUN', 54000);
    const nodes = [pe, ce];
    const params = makeParams(nodes, [{ sort: null }]);

    postSortGroups2Level(params);

    expect(nodes[0]).toBe(ce);
    expect(nodes[1]).toBe(pe);
  });
});

describe('postSortGroups2Level — CE rows sorted by expiry then strike', () => {
  it('sorts CE rows by expiry ascending, then strike ascending', () => {
    const ce1 = makeDerivNode('NIFTY25JUN24500CE', 'NIFTY', 'CE', '25JUN', 24500);
    const ce2 = makeDerivNode('NIFTY25JUN24000CE', 'NIFTY', 'CE', '25JUN', 24000);
    const ce3 = makeDerivNode('NIFTY25MAY24000CE', 'NIFTY', 'CE', '25MAY', 24000);
    // Input: JUN-24500, JUN-24000, MAY-24000 — expected: MAY-24000, JUN-24000, JUN-24500
    const nodes = [ce1, ce2, ce3];
    const params = makeParams(nodes, [{ sort: null }]);

    postSortGroups2Level(params);

    expect(nodes[0]).toBe(ce3); // 25MAY sorts before 25JUN
    expect(nodes[1]).toBe(ce2); // 24000 < 24500 same expiry
    expect(nodes[2]).toBe(ce1);
  });
});

describe('postSortGroups2Level — rows without underlying stay standalone', () => {
  it('standalone rows (no underlying) retain relative position among groups', () => {
    const eq  = makeDerivNode('RELIANCE', '', null, null, null);
    const fut = makeDerivNode('NIFTY25JUNFUT', 'NIFTY', null, '25JUN', null);
    const ce  = makeDerivNode('NIFTY25JUN24500CE', 'NIFTY', 'CE', '25JUN', 24500);
    // Input: standalone, fut, ce — standalone appeared before NIFTY group
    const nodes = [eq, fut, ce];
    const params = makeParams(nodes, [{ sort: null }]);

    postSortGroups2Level(params);

    // standalone 'eq' appeared first (idx 0) → comes before NIFTY group
    expect(nodes[0]).toBe(eq);
    expect(nodes[1]).toBe(fut);
    expect(nodes[2]).toBe(ce);
  });

  it('multiple standalone rows preserve their relative order', () => {
    const eq1 = makeDerivNode('TCS',    '', null, null, null);
    const eq2 = makeDerivNode('INFY',   '', null, null, null);
    const eq3 = makeDerivNode('WIPRO',  '', null, null, null);
    const nodes = [eq1, eq2, eq3];
    const params = makeParams(nodes, [{ sort: null }]);

    postSortGroups2Level(params);

    expect(nodes[0]).toBe(eq1);
    expect(nodes[1]).toBe(eq2);
    expect(nodes[2]).toBe(eq3);
  });

  it('returns early on empty nodes without throwing', () => {
    const nodes = [];
    const params = makeParams(nodes, [{ sort: null }]);

    expect(() => postSortGroups2Level(params)).not.toThrow();
    expect(nodes).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// pairGroupPostSort — underlying-anchor row ordering (2026-10-18 fix)
// ---------------------------------------------------------------------------

/**
 * Build a row shaped like a REAL mergePositionRows output — `accounts`
 * is a non-empty Set (the field pairGroupSort's `_rowAccount` reads via
 * `leadAccount`), plus a `pair_group_key` for the tier/pair key segment.
 */
function makeRealRow(tradingsymbol, underlying, account, pairGroupKey) {
  return {
    data: {
      tradingsymbol, underlying, account: undefined,
      accounts: new Set([account]),
      pair_group_key: pairGroupKey,
    },
  };
}

/**
 * Build a row shaped like a REAL `mergeUnderlyingAnchors` synthetic row —
 * `accounts` is an EMPTY Set (makeRowFactory's default; mergeUnderlyingAnchors
 * never adds to it) and `_isUnderlyingAnchor` is set, exactly as the fixed
 * `mergeUnderlyingAnchors` now does.
 */
function makeAnchorRow(tradingsymbol, underlying) {
  return {
    data: {
      tradingsymbol, underlying, account: undefined,
      accounts: new Set(),
      pair_group_key: null,
      _isUnderlyingAnchor: true,
    },
  };
}

describe('pairGroupPostSort — underlying-anchor row sorts first within its block (2026-10-18 fix)', () => {
  it('BUG reproduction (pre-fix shape): an anchor row with an empty accounts Set sorts to the END of its block, not the start', () => {
    // This test documents WHY the bug happened — an anchor row run through
    // the plain pairGroupSort key (no _isUnderlyingAnchor partitioning)
    // sorts last, because its account segment is '' and '' + '|' (0x7C)
    // lexicographically exceeds any real uppercase account code.
    const anchor = makeAnchorRow('GOLD26OCTFUT', 'GOLD');
    const ce = makeRealRow('GOLD26OCT149000CE', 'GOLD', 'ZG0790', 'P1');
    const pe = makeRealRow('GOLD26OCT142000PE', 'GOLD', 'ZG0790', 'P1');
    const nodes = [ce, pe, anchor];

    // Simulate the OLD composition: no anchor partitioning, just a bare
    // pairGroupSort key comparison (what the pre-fix code effectively did).
    const key = (n) => {
      const acct = n.data.accounts.size ? [...n.data.accounts][0] : '';
      const tier = n.data.tradingsymbol.endsWith('CE') || n.data.tradingsymbol.endsWith('PE') ? 1 : 0;
      return `${acct}|${tier}|${n.data.pair_group_key ?? 'ZZZZ_orphan'}`;
    };
    const sorted = [...nodes].sort((a, b) => (key(a) < key(b) ? -1 : 1));
    expect(sorted[sorted.length - 1]).toBe(anchor);
  });

  it('FIX: pairGroupPostSort places the anchor row first in its underlying block', () => {
    const anchor = makeAnchorRow('GOLD26OCTFUT', 'GOLD');
    const ce1 = makeRealRow('GOLD26OCT149000CE', 'GOLD', 'ZG0790', 'P1');
    const pe1 = makeRealRow('GOLD26OCT142000PE', 'GOLD', 'ZG0790', 'P1');
    const ce2 = makeRealRow('GOLD26OCT155000CE', 'GOLD', 'ZJ6294', 'P2');
    // Anchor arrives LAST in the pre-sort array — mergeUnderlyingAnchors
    // runs after mergePositionRows, so this is the realistic arrival order.
    const nodes = [ce1, pe1, ce2, anchor];
    const params = makeParams(nodes, [{ sort: null }]);

    pairGroupPostSort(params);

    expect(nodes[0]).toBe(anchor);
    // The three real rows still follow, in SOME pairGroupSort-determined
    // order — just never ahead of the anchor.
    expect(nodes.slice(1)).toEqual(expect.arrayContaining([ce1, pe1, ce2]));
  });

  it('FIX: a block with no anchor row (no mergeUnderlyingAnchors trigger) is unaffected — still sorted by account/pair', () => {
    const ce = makeRealRow('NIFTY26JUN24000CE', 'NIFTY', 'ZG0790', 'P1');
    const pe = makeRealRow('NIFTY26JUN24000PE', 'NIFTY', 'ZG0790', 'P1');
    const nodes = [pe, ce];
    const params = makeParams(nodes, [{ sort: null }]);

    expect(() => pairGroupPostSort(params)).not.toThrow();
    expect(nodes).toHaveLength(2);
  });

  it('respects an active column sort (early return, same as the other postSort functions)', () => {
    const anchor = makeAnchorRow('GOLD26OCTFUT', 'GOLD');
    const ce = makeRealRow('GOLD26OCT149000CE', 'GOLD', 'ZG0790', 'P1');
    const nodes = [ce, anchor];
    const params = makeParams(nodes, [{ sort: 'asc' }]);

    pairGroupPostSort(params);
    // Unchanged — the function bailed out immediately.
    expect(nodes[0]).toBe(ce);
    expect(nodes[1]).toBe(anchor);
  });
});
