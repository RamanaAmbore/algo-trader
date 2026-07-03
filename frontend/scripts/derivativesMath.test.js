/**
 * derivativesMath.test.js — Unit tests for pure derivatives math helpers.
 *
 * Run with:  node --test frontend/scripts/derivativesMath.test.js
 *
 * Five quality dimensions (per feedback_test_dimensions.md):
 *  1. SSOT  — computed values match hand-calculated reference values;
 *             greedy-netting fixtures verified by tracing the algorithm.
 *  2. Perf  — no async I/O; all compute is synchronous.
 *  3. Stale — no duplicated closures; every matcher + rollup that was
 *             inline in +page.svelte now lives only here.
 *  4. Reuse — same module path used by +page.svelte imports.
 *  5. UX    — edge cases: empty candidates, zero-qty rows, no spot,
 *             single-leg netting, split-qty residuals.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
  buildAcctMatcher,
  buildStrategyMatcher,
  annotateOptionCandidates,
  computeExpiryBands,
  netMcxGroup,
  assignPairTints,
  bandRowComparator,
  rollupByUnderlying,
  perRootReduce,
} from '../src/lib/data/derivativesMath.js';

// ─────────────────────────────────────────────────────────────────────────────
// Dimension 3 & 4 — SSOT grep: ensure no inline closures survived in page
// ─────────────────────────────────────────────────────────────────────────────
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PAGE_SRC = readFileSync(
  resolve(__dirname, '../src/routes/(algo)/admin/derivatives/+page.svelte'),
  'utf8',
);
const MATH_SRC = readFileSync(
  resolve(__dirname, '../src/lib/data/derivativesMath.js'),
  'utf8',
);

describe('Stale-code checks (Dimension 3)', () => {
  test('derivativesMath.js is the only place _canPair is defined', () => {
    // Count occurrences of the nesting helper function — should be 1 (in module)
    const pageCount = (PAGE_SRC.match(/function _canPair/g) || []).length;
    assert.equal(pageCount, 0, 'No _canPair in +page.svelte after extraction');
    const mathCount = (MATH_SRC.match(/function _canPair/g) || []).length;
    assert.equal(mathCount, 1, '_canPair defined exactly once in derivativesMath.js');
  });

  test('buildAcctMatcher replaces inline _wantedAccts Set pattern in page', () => {
    // The inline pattern: new Set(selectedAccounts.map(...)) followed by matchAccount
    // should only appear in _perRootReduce and minimal remaining spots.
    const inlineSet = (PAGE_SRC.match(/const _wantedAccts = new Set/g) || []).length;
    assert.equal(inlineSet, 0, 'No leftover _wantedAccts inline Set in +page.svelte');
  });

  test('buildStrategyMatcher replaces inline strategy closure pattern', () => {
    // The old pattern: const matchStrategy = (sym) => { if ($selectedStrategyId == null) return true;
    const inlineStrat = (PAGE_SRC.match(/const matchStrategy = \(sym\)/g) || []).length;
    assert.equal(inlineStrat, 0, 'No leftover inline matchStrategy closures in +page.svelte');
  });

  test('computeExpiryBands is the only band-analysis entry point', () => {
    // _eqCloseCounter pattern only in the module (band logic extracted)
    const pageEq = (PAGE_SRC.match(/_eqCloseCounter/g) || []).length;
    assert.equal(pageEq, 0, '_eqCloseCounter not in +page.svelte — moved to module');
    const mathEq = (MATH_SRC.match(/_eqCloseCounter/g) || []).length;
    assert.ok(mathEq >= 1, '_eqCloseCounter in derivativesMath.js');
  });

  test('derivativesMath.js is imported by +page.svelte', () => {
    assert.ok(
      PAGE_SRC.includes("from '$lib/data/derivativesMath.js'"),
      '+page.svelte imports from derivativesMath.js',
    );
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// buildAcctMatcher
// ─────────────────────────────────────────────────────────────────────────────
describe('buildAcctMatcher (Dimension 1 + 5)', () => {
  test('empty selectedAccounts — all pass', () => {
    const m = buildAcctMatcher([]);
    assert.ok(m('ZG0790'));
    assert.ok(m('DH6847'));
    assert.ok(m(null));
    assert.ok(m(undefined));
  });

  test('single account filter', () => {
    const m = buildAcctMatcher(['ZG0790']);
    assert.ok(m('ZG0790'));
    assert.ok(!m('DH6847'));
  });

  test('case-insensitive matching', () => {
    const m = buildAcctMatcher(['zg0790']);
    assert.ok(m('ZG0790'));
    assert.ok(m('zg0790'));
  });

  test('null/undefined account always fails when filter is set', () => {
    const m = buildAcctMatcher(['ZG0790']);
    assert.ok(!m(null));
    assert.ok(!m(undefined));
    assert.ok(!m(''));
  });

  test('whitespace trimming both sides', () => {
    const m = buildAcctMatcher(['  ZG0790  ']);
    assert.ok(m('ZG0790'));
    assert.ok(m('  ZG0790  '));
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// buildStrategyMatcher
// ─────────────────────────────────────────────────────────────────────────────
describe('buildStrategyMatcher (Dimension 1 + 5)', () => {
  test('null strategyId — all pass (fail-open)', () => {
    const m = buildStrategyMatcher(null, new Set(['NIFTY26JUNCE']));
    assert.ok(m('ANYTHING'));
    assert.ok(m(null));
  });

  test('empty openSymbols — fail-open (still loading)', () => {
    const m = buildStrategyMatcher(42, new Set());
    assert.ok(m('NIFTY26JUNCE'));
    assert.ok(m('GARBAGE'));
  });

  test('filters to only symbols in set', () => {
    const m = buildStrategyMatcher(42, new Set(['NIFTY26JUNCE', 'NIFTY26JUNPE']));
    assert.ok(m('NIFTY26JUNCE'));
    assert.ok(m('nifty26junce')); // uppercase normalised
    assert.ok(!m('BANKNIFTY26JUNCE'));
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// annotateOptionCandidates
// ─────────────────────────────────────────────────────────────────────────────

function mkInst(sym, { t = 'CE', k = 100, u = 'NIFTY', x = '2026-06-26' } = {}) {
  return { t, k, u, x };
}
function mkGetInstrument(map) {
  return (sym) => map[sym] || null;
}

describe('annotateOptionCandidates (Dimension 1 + 5)', () => {
  const MCX = new Set(['CRUDEOIL', 'GOLD', 'SILVER', 'GOLDM']);
  const instMap = {
    'NIFTY26JUN24000CE': mkInst('NIFTY26JUN24000CE', { t: 'CE', k: 24000, u: 'NIFTY' }),
    'NIFTY26JUN23000PE': mkInst('NIFTY26JUN23000PE', { t: 'PE', k: 23000, u: 'NIFTY' }),
    'GOLD26JUNFUT':      mkInst('GOLD26JUNFUT',      { t: 'FUT', k: 0,     u: 'GOLD' }),
    'CRUDEOIL26JUN7000CE': mkInst('CRUDEOIL26JUN7000CE', { t: 'CE', k: 7000, u: 'CRUDEOIL' }),
  };

  test('skips futures (t !== CE/PE)', () => {
    const cands = [{ symbol: 'GOLD26JUNFUT', qty: 1, source: 'live' }];
    const out = annotateOptionCandidates({
      candidates: cands, spot: 24000, expFilter: [],
      mcxUnderlyings: MCX, legAnalytics: {}, getInstrument: mkGetInstrument(instMap),
    });
    assert.equal(out.length, 0);
  });

  test('skips draft rows', () => {
    const cands = [{ symbol: 'NIFTY26JUN24000CE', qty: 1, source: 'draft' }];
    const out = annotateOptionCandidates({
      candidates: cands, spot: 24000, expFilter: [],
      mcxUnderlyings: MCX, legAnalytics: {}, getInstrument: mkGetInstrument(instMap),
    });
    assert.equal(out.length, 0);
  });

  test('skips zero-qty when no expiry filter', () => {
    const cands = [{ symbol: 'NIFTY26JUN24000CE', qty: 0, source: 'live' }];
    const out = annotateOptionCandidates({
      candidates: cands, spot: 24000, expFilter: [],
      mcxUnderlyings: MCX, legAnalytics: {}, getInstrument: mkGetInstrument(instMap),
    });
    assert.equal(out.length, 0);
  });

  test('includes zero-qty when expiry filter is set', () => {
    const cands = [{ symbol: 'NIFTY26JUN24000CE', qty: 0, source: 'live' }];
    const out = annotateOptionCandidates({
      candidates: cands, spot: 24000, expFilter: ['2026-06-26'],
      mcxUnderlyings: MCX, legAnalytics: {}, getInstrument: mkGetInstrument(instMap),
    });
    assert.equal(out.length, 1);
    assert.equal(out[0]._qty, 0);
  });

  test('CE is ITM when spot > strike', () => {
    const cands = [{ symbol: 'NIFTY26JUN24000CE', qty: 75, source: 'live' }];
    const out = annotateOptionCandidates({
      candidates: cands, spot: 24500, expFilter: [],
      mcxUnderlyings: MCX, legAnalytics: {}, getInstrument: mkGetInstrument(instMap),
    });
    assert.equal(out.length, 1);
    assert.ok(out[0]._isITM, 'CE above strike should be ITM');
    assert.equal(out[0]._segment, 'equity');
  });

  test('PE is ITM when spot < strike', () => {
    const cands = [{ symbol: 'NIFTY26JUN23000PE', qty: 75, source: 'live' }];
    const out = annotateOptionCandidates({
      candidates: cands, spot: 22000, expFilter: [],
      mcxUnderlyings: MCX, legAnalytics: {}, getInstrument: mkGetInstrument(instMap),
    });
    assert.ok(out[0]._isITM, 'PE with spot < strike should be ITM');
  });

  test('MCX underlying tagged as commodity segment', () => {
    const cands = [{ symbol: 'CRUDEOIL26JUN7000CE', qty: 1, source: 'live' }];
    const out = annotateOptionCandidates({
      candidates: cands, spot: 8000, expFilter: [],
      mcxUnderlyings: MCX, legAnalytics: {}, getInstrument: mkGetInstrument(instMap),
    });
    assert.equal(out[0]._segment, 'commodity');
  });

  test('otmDist is 0 for ITM row', () => {
    const cands = [{ symbol: 'NIFTY26JUN24000CE', qty: 75, source: 'live' }];
    const out = annotateOptionCandidates({
      candidates: cands, spot: 24500, expFilter: [],
      mcxUnderlyings: MCX, legAnalytics: {}, getInstrument: mkGetInstrument(instMap),
    });
    assert.equal(out[0]._otmDist, 0);
  });

  test('otmDist is positive for OTM CE', () => {
    const cands = [{ symbol: 'NIFTY26JUN24000CE', qty: 75, source: 'live' }];
    const out = annotateOptionCandidates({
      candidates: cands, spot: 23500, expFilter: [],
      mcxUnderlyings: MCX, legAnalytics: {}, getInstrument: mkGetInstrument(instMap),
    });
    // CE OTM dist = strike - spot = 24000 - 23500 = 500
    assert.equal(out[0]._otmDist, 500);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// computeExpiryBands (Dimension 1 + 5)
// ─────────────────────────────────────────────────────────────────────────────
describe('computeExpiryBands', () => {
  function mkAnnotated(overrides) {
    return {
      symbol: 'NIFTY26JUN24000CE',
      account: 'ZG0790',
      _strike: 24000, _underlying: 'NIFTY', _expiry: '2026-06-26',
      _optType: 'CE', _segment: 'equity',
      _isITM: true, _spot: 24500, _qty: 75, _theta: -1.2, _otmDist: 0,
      ...overrides,
    };
  }

  test('empty annotated → empty result', () => {
    const r = computeExpiryBands({ annotated: [] });
    assert.deepEqual(r.equity, []);
    assert.deepEqual(r.commodity, []);
  });

  test('ITM equity goes to close band', () => {
    const r = computeExpiryBands({ annotated: [mkAnnotated({ _isITM: true })] });
    assert.equal(r.equity.length, 1);
    assert.equal(r.equity[0]._band, 'close');
    assert.ok(r.equity[0]._closeId.startsWith('C'));
  });

  test('OTM equity goes to otm band', () => {
    const r = computeExpiryBands({
      annotated: [mkAnnotated({ _isITM: false, _otmDist: 300 })],
    });
    assert.equal(r.equity[0]._band, 'otm');
    assert.ok(r.equity[0]._reason.includes('300'));
  });

  test('OTM commodity goes to otm band (not netted)', () => {
    const row = mkAnnotated({ _segment: 'commodity', _isITM: false, _otmDist: 100 });
    const r = computeExpiryBands({ annotated: [row] });
    assert.equal(r.commodity.length, 1);
    assert.equal(r.commodity[0]._band, 'otm');
  });

  test('single ITM commodity goes to close band (no pair partner)', () => {
    const row = mkAnnotated({
      _segment: 'commodity', _isITM: true,
      _optType: 'CE', _qty: 1, _theta: -0.5,
    });
    const r = computeExpiryBands({ annotated: [row] });
    assert.equal(r.commodity.length, 1);
    assert.equal(r.commodity[0]._band, 'close');
  });

  test('pair of ITM commodity CE long + CE short → netted (same type, opposite sign)', () => {
    // Rules 1+2: same opt type + opposite sign → can pair.
    // CE long (qty=+1) + CE short (qty=-1) should net.
    const CElong = mkAnnotated({
      symbol: 'GOLD26JUN7000CE_long', account: 'ZG0790',
      _segment: 'commodity', _isITM: true,
      _optType: 'CE', _qty: 1, _theta: -1.0,
      _underlying: 'GOLD', _expiry: '2026-06-26',
    });
    const CEshort = mkAnnotated({
      symbol: 'GOLD26JUN7000CE_short', account: 'ZG0790',
      _segment: 'commodity', _isITM: true,
      _optType: 'CE', _qty: -1, _theta: -0.8,
      _underlying: 'GOLD', _expiry: '2026-06-26',
    });
    const r = computeExpiryBands({ annotated: [CElong, CEshort] });
    const netted = r.commodity.filter(x => x._band === 'netted');
    assert.equal(netted.length, 2, 'both legs netted');
    assert.equal(netted[0]._pairId, netted[1]._pairId, 'same pairId');
  });

  test('assignPairTints assigns _pairTint to netted rows', () => {
    const rows = [
      { _band: 'netted', _pairId: 'N1' },
      { _band: 'netted', _pairId: 'N1' },
      { _band: 'netted', _pairId: 'N2' },
      { _band: 'close',  _pairId: null },
    ];
    assignPairTints(rows);
    assert.equal(rows[0]._pairTint, 0);
    assert.equal(rows[1]._pairTint, 0);
    assert.equal(rows[2]._pairTint, 1);
    assert.equal(rows[3]._pairTint, undefined, 'close rows have no tint');
  });

  test('pairTint cycles 0-4', () => {
    const rows = Array.from({ length: 10 }, (_, i) => ({
      _band: 'netted', _pairId: `N${i + 1}`,
    }));
    assignPairTints(rows);
    for (let i = 0; i < 10; i++) {
      assert.equal(rows[i]._pairTint, i % 5);
    }
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// netMcxGroup
// ─────────────────────────────────────────────────────────────────────────────
describe('netMcxGroup (Dimension 1 + 5)', () => {
  function mkRow(overrides) {
    return {
      symbol: 'TEST', account: 'ZG0790',
      _optType: 'CE', _qty: 1, _theta: -1.0,
      _underlying: 'TEST', _expiry: '2026-06-26',
      _isITM: true, _segment: 'commodity',
      ...overrides,
    };
  }

  test('single row → no netted, one residual', () => {
    const r = netMcxGroup([mkRow({ _qty: 2 })]);
    assert.equal(r.nettedRows.length, 0);
    assert.equal(r.residuals.length, 1);
    assert.equal(r.residuals[0].qty, 2);
  });

  test('CE+CE same-sign same-type → no netting (Rules 1+2 need opposite sign)', () => {
    const a = mkRow({ _optType: 'CE', _qty: 1, _theta: -1 });
    const b = mkRow({ _optType: 'CE', _qty: 1, _theta: -0.5 });
    const r = netMcxGroup([a, b]);
    // Same type + same sign: _canPair returns false for Rule 1.
    // Different type would need Rule 3 (same sign). Neither applies.
    assert.equal(r.nettedRows.length, 0);
    assert.equal(r.residuals.length, 2);
  });

  test('CE long + CE short → netted pair (same type, opposite sign)', () => {
    const a = mkRow({ _optType: 'CE', _qty:  1, _theta: -1 });
    const b = mkRow({ _optType: 'CE', _qty: -1, _theta: -0.5 });
    const r = netMcxGroup([a, b]);
    assert.equal(r.nettedRows.length, 2);
    assert.equal(r.residuals.length, 0);
  });

  test('theta-priority: higher |theta| gets paired first', () => {
    // Three rows: high-theta pair candidate, low-theta pair candidate, lone row.
    const highTheta = mkRow({ symbol: 'A', _optType: 'CE', _qty:  1, _theta: -3 });
    const lowTheta  = mkRow({ symbol: 'B', _optType: 'CE', _qty:  1, _theta: -0.5 });
    const partner   = mkRow({ symbol: 'C', _optType: 'CE', _qty: -1, _theta: -2 }); // single short
    // Only one short available; it should pair with highTheta (highest |theta|).
    const r = netMcxGroup([highTheta, lowTheta, partner]);
    const nettedSymbols = r.nettedRows.map(n => n.row.symbol);
    assert.ok(nettedSymbols.includes('A'), 'high-theta A gets paired');
    assert.ok(nettedSymbols.includes('C'), 'partner C gets paired');
    // lowTheta should be a residual.
    assert.ok(r.residuals.some(x => x.row.symbol === 'B'), 'low-theta B is residual');
  });

  test('partial qty split: CE(2) + CE(-1) → netted 1 + residual 1', () => {
    const a = mkRow({ _optType: 'CE', _qty:  2, _theta: -1 });
    const b = mkRow({ _optType: 'CE', _qty: -1, _theta: -0.5 });
    const r = netMcxGroup([a, b]);
    // a pairs 1 with b; remaining qty on a is 1
    assert.equal(r.nettedRows.length, 2, 'both emit a netted slice');
    assert.equal(r.residuals.length, 1, 'one residual (remainder of a)');
    assert.equal(r.residuals[0].qty, 1);
    // the netted slice for a should note the split
    const aNetted = r.nettedRows.find(n => n.row === a);
    assert.ok(aNetted.splitNote.length > 0, 'a split note is non-empty');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// bandRowComparator
// ─────────────────────────────────────────────────────────────────────────────
describe('bandRowComparator (Dimension 1)', () => {
  test('close < netted < otm in sort order', () => {
    const rows = [
      { _band: 'otm',    account: 'Z', symbol: 'A' },
      { _band: 'close',  account: 'Z', symbol: 'B' },
      { _band: 'netted', account: 'Z', symbol: 'C' },
    ];
    rows.sort(bandRowComparator);
    assert.equal(rows[0]._band, 'close');
    assert.equal(rows[1]._band, 'netted');
    assert.equal(rows[2]._band, 'otm');
  });

  test('within netted, same pairId rows sort by pairId alpha', () => {
    const rows = [
      { _band: 'netted', account: 'Z', symbol: 'B', _pairId: 'N2' },
      { _band: 'netted', account: 'Z', symbol: 'A', _pairId: 'N1' },
    ];
    rows.sort(bandRowComparator);
    assert.equal(rows[0]._pairId, 'N1');
    assert.equal(rows[1]._pairId, 'N2');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// rollupByUnderlying (Dimension 1 + 5)
// ─────────────────────────────────────────────────────────────────────────────
describe('rollupByUnderlying (Dimension 1 + 5)', () => {
  function mkPos(overrides) {
    return {
      symbol: 'NIFTY26JUN24000CE',
      account: 'ZG0790',
      source: 'live',
      quantity: 75,
      pnl: 1000,
      day_change_val: 100,
      ...overrides,
    };
  }
  function mkHolding(overrides) {
    return {
      symbol: 'NIFTYBEES',
      account: 'ZG0790',
      opening_qty: 100,
      pnl: 500,
      day_change_val: 50,
      ...overrides,
    };
  }

  const decomposeSymbol = (sym) => {
    // Minimal root parser: strip trailing date+CE/PE/FUT
    const m = /^([A-Z]+)/.exec(sym);
    return { root: m ? m[1] : sym };
  };
  const targetsForProxy = (sym) => sym === 'NIFTYBEES' ? ['NIFTY'] : [];
  const getOptionUnderlyingLot = (root) => root === 'NIFTY' ? 50 : 0;
  const baseDayPnlForPosition = (p) => Number(p.day_change_val) || 0;

  test('empty inputs → empty result', () => {
    const out = rollupByUnderlying({
      positions: [], holdings: [], wantedSource: 'live',
      matchAccount: () => true, matchStrategy: () => true,
      filterQ: '', decomposeSymbol, targetsForProxy,
      getOptionUnderlyingLot, baseDayPnlForPosition,
    });
    assert.deepEqual(out, []);
  });

  test('position contributes to F&O group', () => {
    const out = rollupByUnderlying({
      positions: [mkPos({ pnl: 1000, day_change_val: 100 })],
      holdings: [], wantedSource: 'live',
      matchAccount: () => true, matchStrategy: () => true,
      filterQ: '', decomposeSymbol, targetsForProxy,
      getOptionUnderlyingLot, baseDayPnlForPosition,
    });
    assert.equal(out.length, 1);
    assert.equal(out[0].underlying, 'NIFTY');
    assert.equal(out[0].pnl_with, 1000);
    assert.equal(out[0].legs_without, 1);
  });

  test('sim source filtered out when wantedSource=live', () => {
    const out = rollupByUnderlying({
      positions: [mkPos({ source: 'sim' })],
      holdings: [], wantedSource: 'live',
      matchAccount: () => true, matchStrategy: () => true,
      filterQ: '', decomposeSymbol, targetsForProxy,
      getOptionUnderlyingLot, baseDayPnlForPosition,
    });
    assert.equal(out.length, 0);
  });

  test('account filter applied', () => {
    const out = rollupByUnderlying({
      positions: [mkPos({ account: 'DH6847' })],
      holdings: [], wantedSource: 'live',
      matchAccount: buildAcctMatcher(['ZG0790']),
      matchStrategy: () => true,
      filterQ: '', decomposeSymbol, targetsForProxy,
      getOptionUnderlyingLot, baseDayPnlForPosition,
    });
    assert.equal(out.length, 0);
  });

  test('holding proxy credited to target root', () => {
    // NIFTYBEES -> NIFTY via targetsForProxy
    // NIFTY has lot=50, so legs_with += 100/50 = 2
    const out = rollupByUnderlying({
      positions: [mkPos()], // need at least one F&O so legs_without>0
      holdings: [mkHolding({ opening_qty: 100, pnl: 500 })],
      wantedSource: 'live',
      matchAccount: () => true, matchStrategy: () => true,
      filterQ: '', decomposeSymbol, targetsForProxy,
      getOptionUnderlyingLot, baseDayPnlForPosition,
    });
    const nifty = out.find(g => g.underlying === 'NIFTY');
    assert.ok(nifty);
    // pnl_with should include both F&O pnl (1000) and holding pnl (500)
    assert.equal(nifty.pnl_with, 1500);
    // legs_with should include 1 F&O + 2 holding-lot-equiv
    assert.equal(nifty.legs_with, 3);
    // legs_without is F&O only = 1
    assert.equal(nifty.legs_without, 1);
  });

  test('equity-only row hidden (legs_without === 0)', () => {
    // A holding with no F&O position should be excluded from results
    const out = rollupByUnderlying({
      positions: [], // no F&O
      holdings: [mkHolding()],
      wantedSource: 'live',
      matchAccount: () => true, matchStrategy: () => true,
      filterQ: '', decomposeSymbol, targetsForProxy,
      getOptionUnderlyingLot, baseDayPnlForPosition,
    });
    assert.equal(out.length, 0, 'NIFTY hidden: no F&O position present');
  });

  test('filterQ filters underlying names', () => {
    const out = rollupByUnderlying({
      positions: [
        mkPos({ symbol: 'NIFTY26JUN24000CE' }),
        mkPos({ symbol: 'BANKNIFTY26JUN50000CE' }),
      ],
      holdings: [], wantedSource: 'live',
      matchAccount: () => true, matchStrategy: () => true,
      filterQ: 'BANK', decomposeSymbol, targetsForProxy,
      getOptionUnderlyingLot, baseDayPnlForPosition,
    });
    assert.equal(out.length, 1);
    assert.equal(out[0].underlying, 'BANKNIFTY');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// perRootReduce (Dimension 1 + 5)
// ─────────────────────────────────────────────────────────────────────────────
describe('perRootReduce (Dimension 1 + 5)', () => {
  const decomposeSymbol = (sym) => {
    const m = /^([A-Z]+)/.exec(sym);
    return { root: m ? m[1] : sym };
  };
  function mkPos(overrides) {
    return {
      symbol: 'NIFTY26JUN24000CE',
      account: 'ZG0790',
      source: 'live',
      quantity: 75,
      pnl: 1000,
      ...overrides,
    };
  }

  test('empty positions → empty result', () => {
    const out = perRootReduce({
      positions: [], wantedSource: 'live',
      matchAccount: () => true, matchStrategy: () => true,
      decomposeSymbol, getSpot: () => null,
      accessor: (c, _s) => Number(c.pnl || 0),
    });
    assert.deepEqual(out, {});
  });

  test('accessor result summed per root', () => {
    const out = perRootReduce({
      positions: [
        mkPos({ symbol: 'NIFTY26JUN24000CE', pnl: 1000 }),
        mkPos({ symbol: 'NIFTY26JUN23000PE', pnl:  500 }),
      ],
      wantedSource: 'live',
      matchAccount: () => true, matchStrategy: () => true,
      decomposeSymbol, getSpot: () => null,
      accessor: (c, _s) => Number(c.pnl || 0),
    });
    // Both NIFTY prefix → same root
    assert.equal(out['NIFTY'], 1500);
  });

  test('null accessor result skipped', () => {
    const out = perRootReduce({
      positions: [mkPos()],
      wantedSource: 'live',
      matchAccount: () => true, matchStrategy: () => true,
      decomposeSymbol, getSpot: () => null,
      accessor: () => null,
    });
    assert.deepEqual(out, {});
  });

  test('account filter respected', () => {
    const out = perRootReduce({
      positions: [mkPos({ account: 'DH6847' })],
      wantedSource: 'live',
      matchAccount: buildAcctMatcher(['ZG0790']),
      matchStrategy: () => true,
      decomposeSymbol, getSpot: () => null,
      accessor: (c) => Number(c.pnl || 0),
    });
    assert.deepEqual(out, {});
  });

  test('equity positions (not CE/PE/FUT suffix) skipped', () => {
    // Note: 'RELIANCE' ends with 'CE' so it matches /(CE|PE)$/i.
    // Use a symbol that genuinely has no option/future suffix.
    // The code skips rows where neither /FUT$/i nor /(CE|PE)$/i matches.
    const out = perRootReduce({
      positions: [mkPos({ symbol: 'INFY' })],  // pure equity, no CE/PE/FUT suffix
      wantedSource: 'live',
      matchAccount: () => true, matchStrategy: () => true,
      decomposeSymbol, getSpot: () => null,
      accessor: (c) => Number(c.pnl || 0),
    });
    assert.deepEqual(out, {}, 'INFY has no CE/PE/FUT suffix — skipped');
  });

  test('spot passed to accessor', () => {
    const spots = [];
    perRootReduce({
      positions: [mkPos({ underlying_ltp: 0 })],
      wantedSource: 'live',
      matchAccount: () => true, matchStrategy: () => true,
      decomposeSymbol,
      getSpot: (root, _p) => { spots.push(root); return 24000; },
      accessor: (_c, spot) => spot,
    });
    assert.ok(spots.includes('NIFTY'), 'getSpot called with root');
  });
});
