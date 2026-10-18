import { describe, it, expect } from 'vitest';
import { pairGroupSort, sortRowsByPairGroup, pairGroupOrderKey } from '../../data/pairGroupSort.js';
import { positionStateBadge } from '../../data/positionStateBadge.js';

/**
 * Build a minimal IRowNode-like object carrying an `accounts` Set —
 * the MarketPulse row shape (pulseUnified.js:mergePositionRows), as
 * opposed to `makeNode()` below (no account field at all, used by the
 * pre-existing single-account-implied tests).
 * @param {string} symbol
 * @param {string|null} pair_group_key
 * @param {string} account
 */
function makeAcctNode(symbol, pair_group_key, account) {
  return { data: { tradingsymbol: symbol, pair_group_key, accounts: new Set([account]) } };
}

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
// pairGroupSort — account-first ordering (2026-10-17 fix)
//
// Operator: "the positions and legs sort order should be on account and
// then pairs, unpaired showing the last within the account". Before this
// fix, the orphan key was a bare global `'ZZZZ_orphan'` constant with no
// account segment — every account's unpaired rows collapsed to the SAME
// key and clumped together at the very end of the WHOLE grid, instead of
// each account's own orphans sorting to the end of THAT account's own
// cluster. MarketPulse rows carry `data.accounts` (a Set, resolved via
// `leadAccount()`), not a singular `data.account` — makeAcctNode() above
// mirrors that real shape.
// ---------------------------------------------------------------------------

describe('pairGroupSort — account-first ordering (2026-10-17 fix)', () => {
  it('rows are grouped by account FIRST — two accounts never interleave even with matching pair labels', () => {
    const accAp1 = makeAcctNode('A_LEG1', 'P1', 'ACC_A');
    const accBp1 = makeAcctNode('B_LEG1', 'P1', 'ACC_B');
    const accAp2 = makeAcctNode('A_LEG2', 'P2', 'ACC_A');
    const accBp2 = makeAcctNode('B_LEG2', 'P2', 'ACC_B');

    // Interleaved input across accounts.
    const nodes = [accBp1, accAp1, accBp2, accAp2];
    pairGroupSort(nodes);

    // ACC_A's rows (any pair label) must all sort before ACC_B's — account
    // is the primary key, not pair_group_key.
    const accASyms = nodes.filter(n => n.data.tradingsymbol.startsWith('A_')).map(n => n.data.tradingsymbol);
    const accBSyms = nodes.filter(n => n.data.tradingsymbol.startsWith('B_')).map(n => n.data.tradingsymbol);
    const lastAccAIdx = Math.max(...accASyms.map(s => nodes.findIndex(n => n.data.tradingsymbol === s)));
    const firstAccBIdx = Math.min(...accBSyms.map(s => nodes.findIndex(n => n.data.tradingsymbol === s)));
    expect(lastAccAIdx).toBeLessThan(firstAccBIdx);
  });

  it("an account's own orphan row sorts to the end of THAT account's cluster, not to the very end of the whole grid", () => {
    const accAp1    = makeAcctNode('A_PAIRED', 'P1', 'ACC_A');
    const accAorphan = makeAcctNode('A_ORPHAN', null, 'ACC_A');
    const accBp1    = makeAcctNode('B_PAIRED', 'P2', 'ACC_B'); // global numbering — different label, same account

    // ACC_A's orphan must sort BEFORE ACC_B's rows entirely (account is
    // primary), even though it's last WITHIN ACC_A's own cluster.
    const nodes = [accBp1, accAorphan, accAp1];
    pairGroupSort(nodes);

    const symbols = nodes.map(n => n.data.tradingsymbol);
    expect(symbols).toEqual(['A_PAIRED', 'A_ORPHAN', 'B_PAIRED']);
  });

  it('two different accounts each with ONLY an orphan row do not clump together at the global end', () => {
    // This is the exact bug: before the fix, both orphans keyed to the
    // same bare 'ZZZZ_orphan' constant and could land adjacent to each
    // other regardless of account, instead of each sorting within its
    // own account's position in the overall account ordering.
    const accAp1     = makeAcctNode('A_PAIRED', 'P1', 'ACC_A');
    const accAorphan = makeAcctNode('A_ORPHAN', null, 'ACC_A');
    const accBorphan = makeAcctNode('B_ORPHAN', null, 'ACC_B');

    const nodes = [accBorphan, accAorphan, accAp1];
    pairGroupSort(nodes);

    // ACC_A's cluster (paired then orphan) stays together and sorts
    // before ACC_B's orphan — not "all orphans together regardless of account".
    const symbols = nodes.map(n => n.data.tradingsymbol);
    expect(symbols).toEqual(['A_PAIRED', 'A_ORPHAN', 'B_ORPHAN']);
  });
});

// ---------------------------------------------------------------------------
// pairGroupSort / sortRowsByPairGroup — tier-aware ordering (2026-10-17
// same-day follow-up fix): the underlying/future sorts ABOVE its own
// options within an account's cluster. Before this fix, a lone future
// (which almost always has no pair_group_key, since it can only
// waterfall-pair against another future of the same root) fell into the
// 'ZZZZ_orphan' bucket and sorted to the END of the account's cluster —
// e.g. "gold showing below [its options], should be first as an
// underlying". Real parseable symbols are required here (decomposeSymbol
// drives tier derivation) — synthetic symbols like 'SYMA'/'FIRST' used
// elsewhere in this file all resolve to kind='eq' -> tier 0, so they
// never exercised this path.
// ---------------------------------------------------------------------------

describe('pairGroupOrderKey — tier derivation', () => {
  it('a future symbol gets tier 0, ahead of any option tier 1', () => {
    const futKey = pairGroupOrderKey({ tradingsymbol: 'CRUDEOIL25JUNFUT', pair_group_key: null });
    const optKey = pairGroupOrderKey({ tradingsymbol: 'NIFTY25JUN24000CE', pair_group_key: null });
    expect(futKey.startsWith('0|')).toBe(true);
    expect(optKey.startsWith('1|')).toBe(true);
    expect(futKey < optKey).toBe(true);
  });

  it('resolves symbol from either `tradingsymbol` (MarketPulse) or `symbol` (Legs/Exp-Close) field', () => {
    const viaTradingsymbol = pairGroupOrderKey({ tradingsymbol: 'NIFTY25JUN24000CE', pair_group_key: 'P1' });
    const viaSymbol = pairGroupOrderKey({ symbol: 'NIFTY25JUN24000CE', pair_group_key: 'P1' });
    expect(viaTradingsymbol).toBe(viaSymbol);
    expect(viaTradingsymbol).toBe('1|P1');
  });

  it('a row with no resolvable symbol falls back to tier 0 (eq/unparseable), not a thrown error', () => {
    expect(() => pairGroupOrderKey({})).not.toThrow();
    expect(pairGroupOrderKey({}).startsWith('0|')).toBe(true);
  });
});

describe('pairGroupSort — underlying/future sorts above its own options (2026-10-17)', () => {
  it('a lone future row sorts BEFORE P1/P2/orphan option rows in the same account', () => {
    const fut    = makeAcctNode('CRUDEOIL25JUNFUT', null, 'ACC_A');
    const p1     = makeAcctNode('CRUDEOIL25JUN8000CE', 'P1', 'ACC_A');
    const p2     = makeAcctNode('CRUDEOIL25JUN8500PE', 'P2', 'ACC_A');
    const orphan = makeAcctNode('CRUDEOIL25JUN9000CE', null, 'ACC_A');

    // Input order deliberately puts the future last to prove the sort
    // moves it, not that it happened to already be first.
    const nodes = [orphan, p2, p1, fut];
    pairGroupSort(nodes);

    expect(nodes[0]).toBe(fut);
    expect(nodes[1]).toBe(p1);
    expect(nodes[2]).toBe(p2);
    expect(nodes[3]).toBe(orphan);
  });

  it('each account keeps its own future ahead of its own options — no cross-account leak (composes with account-first invariant)', () => {
    const futA = makeAcctNode('GOLDM25DECFUT', null, 'ACC_A');
    const optA = makeAcctNode('GOLDM25DEC75000CE', 'P1', 'ACC_A');
    const futB = makeAcctNode('GOLDM25DECFUT', null, 'ACC_B');
    const optB = makeAcctNode('GOLDM25DEC75000PE', 'P1', 'ACC_B');

    const nodes = [optB, optA, futB, futA];
    pairGroupSort(nodes);

    const syms = nodes.map(n => n.data.tradingsymbol + '|' + [...n.data.accounts][0]);
    // ACC_A's cluster (future then option) stays fully together and
    // before ACC_B's cluster — account remains the primary segment.
    expect(syms).toEqual([
      'GOLDM25DECFUT|ACC_A', 'GOLDM25DEC75000CE|ACC_A',
      'GOLDM25DECFUT|ACC_B', 'GOLDM25DEC75000PE|ACC_B',
    ]);
  });

  it('pair-adjacency among options is unchanged by the tier fix — a pair\'s two legs stay adjacent regardless of a future being present', () => {
    const fut  = makeAcctNode('NIFTY25JUNFUT', null, 'ACC_A');
    const p1a  = makeAcctNode('NIFTY25JUN24000CE', 'P1', 'ACC_A');
    const p1b  = makeAcctNode('NIFTY25JUN25000PE', 'P1', 'ACC_A');

    const nodes = [p1b, fut, p1a];
    pairGroupSort(nodes);

    expect(nodes[0]).toBe(fut);
    // P1's two legs are adjacent (positions 1 and 2), in original
    // relative order — the future's presence doesn't split the pair.
    expect(nodes[1]).toBe(p1b);
    expect(nodes[2]).toBe(p1a);
  });
});

describe('sortRowsByPairGroup — underlying/future sorts above its own options (2026-10-17)', () => {
  function row(symbol, pair_group_key = null, account = 'ACC1') {
    return { symbol, pair_group_key, account };
  }

  it('a lone future leg sorts BEFORE P1/orphan option legs in the same account', () => {
    const fut    = row('CRUDEOIL25JUNFUT', null, 'ACC_A');
    const p1     = row('CRUDEOIL25JUN8000CE', 'P1', 'ACC_A');
    const orphan = row('CRUDEOIL25JUN9000CE', null, 'ACC_A');

    const sorted = sortRowsByPairGroup([orphan, p1, fut]);

    expect(sorted.map(r => r.symbol)).toEqual([
      'CRUDEOIL25JUNFUT', 'CRUDEOIL25JUN8000CE', 'CRUDEOIL25JUN9000CE',
    ]);
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

  it("an account's own orphan leg sorts to the end of THAT account's cluster, not the global end of the grid (2026-10-17 fix)", () => {
    const accAPaired = row('A_PAIRED', 'P1', 'ACC_A');
    const accAOrphan = row('A_ORPHAN', null, 'ACC_A');
    const accBPaired = row('B_PAIRED', 'P2', 'ACC_B'); // global numbering, different account

    const sorted = sortRowsByPairGroup([accBPaired, accAOrphan, accAPaired]);

    expect(sorted.map(r => r.symbol)).toEqual(['A_PAIRED', 'A_ORPHAN', 'B_PAIRED']);
  });

  it('two accounts each with only an orphan leg do not clump together — each sorts within its own account position', () => {
    const accAPaired = row('A_PAIRED', 'P1', 'ACC_A');
    const accAOrphan = row('A_ORPHAN', null, 'ACC_A');
    const accBOrphan = row('B_ORPHAN', null, 'ACC_B');

    const sorted = sortRowsByPairGroup([accBOrphan, accAOrphan, accAPaired]);

    expect(sorted.map(r => r.symbol)).toEqual(['A_PAIRED', 'A_ORPHAN', 'B_ORPHAN']);
  });
});
