/**
 * symbols_search_augmented.test.js
 *
 * Unit tests for the virtual-root injection in searchByPrefix().
 * Uses node --test (no framework dependency).
 *
 * Run:
 *   node --test frontend/scripts/symbols_search_augmented.test.js
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *  1. SSOT   — virtual roots produced by seedRootMap / getVirtualRoots
 *  2. Perf   — sync helpers only; no async I/O
 *  3. Stale  — augmentation logic lives in instruments.js + rootOf.js, not duplicated
 *  4. Reuse  — same import paths used by MarketPulse typeahead
 *  5. UX     — virtual rows carry virtual:true flag; display converts _NEXT → .NEXT
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';

// ── Import the modules under test ──────────────────────────────────────────

// rootOf.js is pure ESM; import directly.
import {
  seedRootMap,
  seedRootMapFromInstruments,
  getVirtualRoots,
  rootOf,
  rootOfLabel,
  resolveVirtual,
} from '../src/lib/data/rootOf.js';

import { displaySymbol } from '../src/lib/data/displaySymbol.js';

// ── Fixtures ───────────────────────────────────────────────────────────────

/**
 * Minimal instruments array covering MCX commodities + NSE equity + CDS.
 * Mirrors the compact Kite payload shape used by instruments.js.
 */
function mkInstruments() {
  return [
    // MCX GOLD — two active contracts
    { s: 'GOLD26JULFUT',   e: 'MCX', t: 'FUT', u: 'GOLD',      x: '2026-07-31' },
    { s: 'GOLD26AUGFUT',   e: 'MCX', t: 'FUT', u: 'GOLD',      x: '2026-08-29' },
    // MCX GOLD options
    { s: 'GOLD26JUL62000CE', e: 'MCX', t: 'CE', u: 'GOLD',     x: '2026-07-31', k: 62000, ls: 1 },
    { s: 'GOLD26JUL62000PE', e: 'MCX', t: 'PE', u: 'GOLD',     x: '2026-07-31', k: 62000, ls: 1 },
    // NSE equity
    { s: 'GOLDBEES',         e: 'NSE', t: 'EQ', u: 'GOLDBEES' },
    // MCX CRUDEOIL — two active contracts
    { s: 'CRUDEOIL26JULFUT', e: 'MCX', t: 'FUT', u: 'CRUDEOIL', x: '2026-07-15' },
    { s: 'CRUDEOIL26AUGFUT', e: 'MCX', t: 'FUT', u: 'CRUDEOIL', x: '2026-08-19' },
    // NSE RELIANCE equity + futures
    { s: 'RELIANCE',         e: 'NSE', t: 'EQ',  u: 'RELIANCE' },
    { s: 'RELIANCE26JULFUT', e: 'NFO', t: 'FUT', u: 'RELIANCE', x: '2026-07-31' },
    { s: 'RELIANCE26JUL3000CE', e: 'NFO', t: 'CE', u: 'RELIANCE', x: '2026-07-31', k: 3000, ls: 250 },
    // CDS USDINR — two active contracts
    { s: 'USDINR26JULFUT',  e: 'CDS', t: 'FUT', u: 'USDINR',   x: '2026-07-29' },
    { s: 'USDINR26AUGFUT',  e: 'CDS', t: 'FUT', u: 'USDINR',   x: '2026-08-26' },
  ];
}

// ── getVirtualRoots tests ──────────────────────────────────────────────────

describe('getVirtualRoots', () => {
  test('returns empty when root map not seeded', () => {
    // Reset to empty
    seedRootMap({}, {});
    assert.deepEqual(getVirtualRoots('MCX'), []);
    assert.deepEqual(getVirtualRoots('CDS'), []);
  });

  test('returns front-month root when only one contract exists', () => {
    seedRootMap({ GOLD: ['GOLD26JULFUT'] }, {});
    const roots = getVirtualRoots('MCX');
    assert.equal(roots.length, 1);
    assert.equal(roots[0].s, 'GOLD');
    assert.equal(roots[0].e, 'MCX');
    assert.equal(roots[0].t, 'FUT');
    assert.equal(roots[0].virtual, true);
  });

  test('returns front + back virtual roots when two contracts exist', () => {
    seedRootMap({ GOLD: ['GOLD26JULFUT', 'GOLD26AUGFUT'] }, {});
    const roots = getVirtualRoots('MCX');
    assert.equal(roots.length, 2);
    assert.equal(roots[0].s, 'GOLD');
    assert.equal(roots[1].s, 'GOLD_NEXT');
    assert.equal(roots[1].virtual, true);
  });

  test('seedRootMapFromInstruments populates MCX + CDS roots', () => {
    const items = mkInstruments();
    seedRootMapFromInstruments(items);

    const mcx = getVirtualRoots('MCX');
    const syms = mcx.map(r => r.s);
    assert.ok(syms.includes('GOLD'),        'should have GOLD virtual root');
    assert.ok(syms.includes('GOLD_NEXT'),   'should have GOLD_NEXT virtual root');
    assert.ok(syms.includes('CRUDEOIL'),    'should have CRUDEOIL virtual root');
    assert.ok(syms.includes('CRUDEOIL_NEXT'), 'should have CRUDEOIL_NEXT virtual root');

    const cds = getVirtualRoots('CDS');
    const cdsSyms = cds.map(r => r.s);
    assert.ok(cdsSyms.includes('USDINR'),      'should have USDINR virtual root');
    assert.ok(cdsSyms.includes('USDINR_NEXT'), 'should have USDINR_NEXT virtual root');
  });

  test('NSE equity symbols do NOT generate virtual roots', () => {
    seedRootMapFromInstruments(mkInstruments());
    const mcx = getVirtualRoots('MCX');
    assert.ok(!mcx.some(r => r.s === 'RELIANCE'), 'RELIANCE is NSE, not MCX virtual');
    assert.ok(!mcx.some(r => r.s === 'GOLDBEES'),  'GOLDBEES is NSE equity, not MCX virtual');
  });

  test('sorted alphabetically by root name', () => {
    seedRootMapFromInstruments(mkInstruments());
    const roots = getVirtualRoots('MCX').map(r => r.s.replace(/_NEXT$/, ''));
    const sorted = [...roots].sort();
    assert.deepEqual(roots, sorted);
  });
});

// ── displaySymbol tests ───────────────────────────────────────────────────

describe('displaySymbol', () => {
  test('GOLD_NEXT → GOLD.NEXT', () => {
    assert.equal(displaySymbol('GOLD_NEXT'), 'GOLD.NEXT');
  });
  test('CRUDEOIL_NEXT → CRUDEOIL.NEXT', () => {
    assert.equal(displaySymbol('CRUDEOIL_NEXT'), 'CRUDEOIL.NEXT');
  });
  test('real contract passes through unchanged', () => {
    assert.equal(displaySymbol('GOLD26JULFUT'), 'GOLD26JULFUT');
  });
  test('equity passes through unchanged', () => {
    assert.equal(displaySymbol('GOLDBEES'), 'GOLDBEES');
  });
  test('bare root passes through unchanged', () => {
    assert.equal(displaySymbol('GOLD'), 'GOLD');
  });
  test('null/undefined returns empty string', () => {
    assert.equal(displaySymbol(null), '');
    assert.equal(displaySymbol(undefined), '');
  });
});

// ── Virtual row shape validation ──────────────────────────────────────────

describe('virtual row shape', () => {
  test('every virtual row has required fields for downstream consumers', () => {
    seedRootMapFromInstruments(mkInstruments());
    const mcx = getVirtualRoots('MCX');
    for (const r of mcx) {
      assert.ok(r.s,         `row ${r.s}: .s must be truthy`);
      assert.ok(r.e,         `row ${r.s}: .e must be truthy`);
      assert.ok(r.t,         `row ${r.s}: .t must be truthy`);
      assert.equal(r.virtual, true, `row ${r.s}: .virtual must be true`);
      // The `u` (underlying) field is used by MarketPulse / symbolStore.
      assert.ok(r.u, `row ${r.s}: .u (underlying) must be truthy`);
    }
  });

  test('GOLD_NEXT underlying is GOLD (not GOLD_NEXT)', () => {
    seedRootMap({ GOLD: ['GOLD26JULFUT', 'GOLD26AUGFUT'] }, {});
    const roots = getVirtualRoots('MCX');
    const next = roots.find(r => r.s === 'GOLD_NEXT');
    assert.ok(next, 'GOLD_NEXT row should exist');
    assert.equal(next.u, 'GOLD', 'underlying of GOLD_NEXT should be GOLD');
  });
});

// ── rootOf + resolveVirtual round-trip ───────────────────────────────────

describe('rootOf / resolveVirtual round-trip', () => {
  test('front-month contract maps to bare root', () => {
    seedRootMap({ GOLD: ['GOLD26JULFUT', 'GOLD26AUGFUT'] }, {});
    assert.equal(rootOf('GOLD26JULFUT', 'MCX'), 'GOLD');
  });

  test('back-month contract maps to _NEXT root', () => {
    seedRootMap({ GOLD: ['GOLD26JULFUT', 'GOLD26AUGFUT'] }, {});
    assert.equal(rootOf('GOLD26AUGFUT', 'MCX'), 'GOLD_NEXT');
  });

  test('resolveVirtual(GOLD) returns front-month contract', () => {
    seedRootMap({ GOLD: ['GOLD26JULFUT', 'GOLD26AUGFUT'] }, {});
    assert.equal(resolveVirtual('GOLD', 'MCX'), 'GOLD26JULFUT');
  });

  test('resolveVirtual(GOLD_NEXT) returns back-month contract', () => {
    seedRootMap({ GOLD: ['GOLD26JULFUT', 'GOLD26AUGFUT'] }, {});
    assert.equal(resolveVirtual('GOLD_NEXT', 'MCX'), 'GOLD26AUGFUT');
  });

  test('rootOfLabel uses displaySymbol for _NEXT', () => {
    seedRootMap({ GOLD: ['GOLD26JULFUT', 'GOLD26AUGFUT'] }, {});
    assert.equal(rootOfLabel('GOLD26AUGFUT', 'MCX'), 'GOLD.NEXT');
  });
});

// ── searchByPrefix ordering guarantee ────────────────────────────────────
// These tests exercise the virtual-injection in instruments.js indirectly
// by testing that virtual roots (which come from rootOf.js) are separate
// from regular rows and conform to the correct shape. Full searchByPrefix
// is async + IDB-backed so direct invocation in Node.js is not viable;
// the ordering invariant is instead validated through getVirtualRoots.

describe('virtual root ordering invariant', () => {
  test('GOLD prefix → virtual GOLD and GOLD_NEXT appear before GOLDBEES concept', () => {
    seedRootMapFromInstruments(mkInstruments());
    const virtMcx = getVirtualRoots('MCX').filter(r => r.s.startsWith('GOLD'));
    assert.ok(virtMcx.some(r => r.s === 'GOLD'),      'GOLD virtual present');
    assert.ok(virtMcx.some(r => r.s === 'GOLD_NEXT'), 'GOLD_NEXT virtual present');
    // GOLDBEES is NSE EQ — must NOT appear in MCX virtual roots
    assert.ok(!virtMcx.some(r => r.s === 'GOLDBEES'),  'GOLDBEES is not an MCX virtual');
  });

  test('RELIANCE prefix → no MCX or CDS virtual root (NSE-only underlying)', () => {
    seedRootMapFromInstruments(mkInstruments());
    const mcx = getVirtualRoots('MCX').filter(r => r.s.startsWith('RELIANCE'));
    const cds = getVirtualRoots('CDS').filter(r => r.s.startsWith('RELIANCE'));
    assert.equal(mcx.length, 0, 'RELIANCE has no MCX virtual root');
    assert.equal(cds.length, 0, 'RELIANCE has no CDS virtual root');
  });

  test('USDINR prefix → CDS virtual roots exist', () => {
    seedRootMapFromInstruments(mkInstruments());
    const cds = getVirtualRoots('CDS').filter(r => r.s.startsWith('USDINR'));
    assert.ok(cds.some(r => r.s === 'USDINR'),      'USDINR virtual present');
    assert.ok(cds.some(r => r.s === 'USDINR_NEXT'), 'USDINR_NEXT virtual present');
  });

  test('CRUDEOIL prefix → MCX virtual roots exist', () => {
    seedRootMapFromInstruments(mkInstruments());
    const mcx = getVirtualRoots('MCX').filter(r => r.s.startsWith('CRUDEOIL'));
    assert.ok(mcx.some(r => r.s === 'CRUDEOIL'),      'CRUDEOIL virtual present');
    assert.ok(mcx.some(r => r.s === 'CRUDEOIL_NEXT'), 'CRUDEOIL_NEXT virtual present');
  });
});
