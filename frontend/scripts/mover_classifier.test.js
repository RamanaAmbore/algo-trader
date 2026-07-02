/**
 * mover_classifier.test.js — Unit tests for the multi-tab mover classifier.
 *
 * Run with:  node --test frontend/scripts/mover_classifier.test.js
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *  1. SSOT  — classifier rules match hand-verified examples (RELIANCE/BHEL/SENSEX)
 *  2. Perf  — no async I/O; pure Set lookups are synchronous O(1)
 *  3. Stale — classification logic lives in marketDataStores; test reads
 *             source-of-truth sets from indexConstituents.js directly
 *  4. Reuse — same Set exports that the store uses (same import paths)
 *  5. UX    — edge cases: unknown symbol, empty string, mixed-case input
 *
 * NOTE: marketDataStores.svelte.js uses Svelte 5 runes ($state etc.) and
 * cannot be imported directly by Node. The classifier is tested here by
 * re-implementing it against the same source sets so any drift in
 * indexConstituents.js is caught at test time.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
  FO_UNDERLYINGS,
  FO_STOCK_UNDERLYINGS,
  NIFTY_MIDCAP_100,
  NIFTY_SMLCAP_100,
} from '../src/lib/data/indexConstituents.js';

// ── Replicate _classifyMoverGroups from marketDataStores.svelte.js ───────────
// Keep this in sync with the store implementation.  If you change the
// classifier in the store, update the reference implementation here too.
//
// Membership rules (non-exclusive):
// - 'underlying' = any F&O underlying (FO_UNDERLYINGS)
// - 'large_cap'  = FO_STOCK_UNDERLYINGS minus MIDCAP/SMLCAP members
// - 'midcap'     = NIFTY MIDCAP 100 constituent
// - 'smallcap'   = NIFTY SMLCAP 100 constituent
function classifyMoverGroups(sym) {
  const s = String(sym || '').toUpperCase();
  const groups = [];
  if (FO_UNDERLYINGS.has(s))        groups.push('underlying');
  if (FO_STOCK_UNDERLYINGS.has(s)
      && !NIFTY_MIDCAP_100.has(s)
      && !NIFTY_SMLCAP_100.has(s))  groups.push('large_cap');
  if (NIFTY_MIDCAP_100.has(s))      groups.push('midcap');
  if (NIFTY_SMLCAP_100.has(s))      groups.push('smallcap');
  return groups;
}

// Store-layer default: unknown symbols fall back to ['underlying']
function effectiveGroups(sym) {
  const g = classifyMoverGroups(sym);
  return g.length > 0 ? g : ['underlying'];
}

// ── Multi-tab overlap tests ───────────────────────────────────────────────────

describe('_classifyMoverGroups — multi-tab membership', () => {
  test('RELIANCE appears in underlying AND large_cap', () => {
    const g = classifyMoverGroups('RELIANCE');
    assert.ok(g.includes('underlying'), 'RELIANCE must be in underlying');
    assert.ok(g.includes('large_cap'),  'RELIANCE must be in large_cap');
    assert.ok(!g.includes('midcap'),    'RELIANCE must NOT be in midcap');
    assert.ok(!g.includes('smallcap'),  'RELIANCE must NOT be in smallcap');
  });

  test('BHEL appears in underlying AND midcap (not large_cap)', () => {
    const g = classifyMoverGroups('BHEL');
    assert.ok(g.includes('underlying'), 'BHEL must be in underlying');
    assert.ok(g.includes('midcap'),     'BHEL must be in midcap');
    assert.ok(!g.includes('large_cap'), 'BHEL must NOT be in large_cap');
    assert.ok(!g.includes('smallcap'),  'BHEL must NOT be in smallcap');
  });

  test('SENSEX appears ONLY in underlying (BSE index, not large_cap)', () => {
    const g = classifyMoverGroups('SENSEX');
    assert.deepStrictEqual(g, ['underlying'],
      'SENSEX must be underlying-only');
  });

  test('BANKEX appears ONLY in underlying', () => {
    const g = classifyMoverGroups('BANKEX');
    assert.deepStrictEqual(g, ['underlying'],
      'BANKEX must be underlying-only');
  });

  test('TCS appears in underlying AND large_cap', () => {
    const g = classifyMoverGroups('TCS');
    assert.ok(g.includes('underlying'), 'TCS must be in underlying');
    assert.ok(g.includes('large_cap'),  'TCS must be in large_cap');
  });

  test('HDFCBANK appears in underlying AND large_cap', () => {
    const g = classifyMoverGroups('HDFCBANK');
    assert.ok(g.includes('underlying'), 'HDFCBANK must be in underlying');
    assert.ok(g.includes('large_cap'),  'HDFCBANK must be in large_cap');
  });
});

// ── Midcap F&O overlap ────────────────────────────────────────────────────────

describe('_classifyMoverGroups — midcap F&O overlap', () => {
  // BHEL, CGPOWER, NMDC, SAIL, RECLTD etc. are in both FO_UNDERLYINGS
  // and NIFTY_MIDCAP_100 — they should show in underlying + midcap tabs.
  const MIDCAP_FO_NAMES = ['BHEL', 'CGPOWER', 'NMDC', 'SAIL', 'RECLTD', 'IRCTC', 'CHOLAFIN'];
  for (const sym of MIDCAP_FO_NAMES) {
    test(`${sym} in both underlying and midcap`, () => {
      const g = classifyMoverGroups(sym);
      if (!FO_UNDERLYINGS.has(sym)) {
        // Not in F&O list — skip underlying assertion, only midcap check matters
        assert.ok(g.includes('midcap'), `${sym} must be in midcap`);
      } else {
        assert.ok(g.includes('underlying'), `${sym} must be in underlying`);
        assert.ok(g.includes('midcap'),     `${sym} must be in midcap`);
        assert.ok(!g.includes('large_cap'), `${sym} must NOT be in large_cap`);
      }
    });
  }
});

// ── Edge cases ────────────────────────────────────────────────────────────────

describe('_classifyMoverGroups — edge cases', () => {
  test('unknown symbol returns empty array', () => {
    const g = classifyMoverGroups('UNKNOWN_SYM_XYZ');
    assert.deepStrictEqual(g, []);
  });

  test('empty string returns empty array', () => {
    const g = classifyMoverGroups('');
    assert.deepStrictEqual(g, []);
  });

  test('null/undefined input returns empty array', () => {
    assert.deepStrictEqual(classifyMoverGroups(null), []);
    assert.deepStrictEqual(classifyMoverGroups(undefined), []);
  });

  test('case-insensitive input normalised to uppercase', () => {
    const lower = classifyMoverGroups('reliance');
    const upper = classifyMoverGroups('RELIANCE');
    assert.deepStrictEqual(lower, upper);
  });

  test('store-layer default: unknown symbol effective group is [underlying]', () => {
    assert.deepStrictEqual(effectiveGroups('UNKNOWN_SYM_XYZ'), ['underlying']);
  });

  test('store-layer default: known symbol keeps its real groups', () => {
    const g = effectiveGroups('RELIANCE');
    assert.ok(g.includes('underlying'));
    assert.ok(g.includes('large_cap'));
  });
});

// ── Tab-count logic verification ──────────────────────────────────────────────

describe('tab counts — multi-membership inflates per-tab counts', () => {
  test('RELIANCE counted in BOTH underlying and large_cap tab totals', () => {
    const rows = [
      { _moverGroups: classifyMoverGroups('RELIANCE'), _moverDirection: 'losers' },
    ];
    const counts = { underlying: 0, large_cap: 0, midcap: 0, smallcap: 0 };
    for (const r of rows) {
      for (const g of r._moverGroups) {
        if (g in counts) counts[g]++;
      }
    }
    assert.strictEqual(counts.underlying, 1, 'underlying count should be 1');
    assert.strictEqual(counts.large_cap,  1, 'large_cap count should be 1');
    assert.strictEqual(counts.midcap,     0, 'midcap count should be 0');
  });

  test('BHEL counted in BOTH underlying and midcap tab totals', () => {
    const rows = [
      { _moverGroups: classifyMoverGroups('BHEL'), _moverDirection: 'losers' },
    ];
    const counts = { underlying: 0, large_cap: 0, midcap: 0, smallcap: 0 };
    for (const r of rows) {
      for (const g of r._moverGroups) {
        if (g in counts) counts[g]++;
      }
    }
    assert.strictEqual(counts.underlying, 1, 'underlying count should be 1');
    assert.strictEqual(counts.large_cap,  0, 'large_cap count should be 0 for BHEL');
    assert.strictEqual(counts.midcap,     1, 'midcap count should be 1');
  });

  test('SENSEX counted ONLY in underlying tab total', () => {
    const rows = [
      { _moverGroups: classifyMoverGroups('SENSEX'), _moverDirection: 'winners' },
    ];
    const counts = { underlying: 0, large_cap: 0, midcap: 0, smallcap: 0 };
    for (const r of rows) {
      for (const g of r._moverGroups) {
        if (g in counts) counts[g]++;
      }
    }
    assert.strictEqual(counts.underlying, 1);
    assert.strictEqual(counts.large_cap,  0);
    assert.strictEqual(counts.midcap,     0);
    assert.strictEqual(counts.smallcap,   0);
  });

  test('mixed pool: tab counts are additive, not exclusive', () => {
    const syms = ['RELIANCE', 'BHEL', 'SENSEX', 'TCS'];
    const rows = syms.map(sym => ({
      _moverGroups: effectiveGroups(sym),
      _moverDirection: 'losers',
    }));
    const counts = { underlying: 0, large_cap: 0, midcap: 0, smallcap: 0 };
    for (const r of rows) {
      for (const g of r._moverGroups) {
        if (g in counts) counts[g]++;
      }
    }
    // RELIANCE + BHEL + SENSEX + TCS all in underlying = 4
    assert.strictEqual(counts.underlying, 4);
    // RELIANCE + TCS in large_cap = 2; BHEL and SENSEX excluded
    assert.strictEqual(counts.large_cap,  2);
    // BHEL in midcap = 1
    assert.strictEqual(counts.midcap, 1);
  });
});

// ── Source-set sanity checks ──────────────────────────────────────────────────

describe('indexConstituents.js — source set sanity', () => {
  test('SENSEX is NOT in FO_STOCK_UNDERLYINGS (excluded by _BSE_SINGLE_TOKEN_INDICES)', () => {
    assert.ok(!FO_STOCK_UNDERLYINGS.has('SENSEX'),
      'SENSEX must not be in FO_STOCK_UNDERLYINGS');
  });

  test('BANKEX is NOT in FO_STOCK_UNDERLYINGS', () => {
    assert.ok(!FO_STOCK_UNDERLYINGS.has('BANKEX'),
      'BANKEX must not be in FO_STOCK_UNDERLYINGS');
  });

  test('SENSEX IS in FO_UNDERLYINGS', () => {
    assert.ok(FO_UNDERLYINGS.has('SENSEX'), 'SENSEX must be in FO_UNDERLYINGS');
  });

  test('RELIANCE is in FO_STOCK_UNDERLYINGS', () => {
    assert.ok(FO_STOCK_UNDERLYINGS.has('RELIANCE'));
  });

  test('BHEL is in FO_UNDERLYINGS and NIFTY_MIDCAP_100', () => {
    assert.ok(FO_UNDERLYINGS.has('BHEL'));
    assert.ok(NIFTY_MIDCAP_100.has('BHEL'));
  });

  test('All FO_STOCK_UNDERLYINGS are a subset of FO_UNDERLYINGS', () => {
    for (const sym of FO_STOCK_UNDERLYINGS) {
      assert.ok(FO_UNDERLYINGS.has(sym),
        `${sym} in FO_STOCK_UNDERLYINGS must also be in FO_UNDERLYINGS`);
    }
  });
});
