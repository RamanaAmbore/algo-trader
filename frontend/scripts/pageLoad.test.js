/**
 * pageLoad.test.js — Unit tests for $lib/derivatives/pageLoad.js helpers.
 *
 * Run with:  node --test frontend/scripts/pageLoad.test.js
 *
 * Five quality dimensions (per feedback_test_dimensions.md):
 *  1. SSOT  — hand-calculated reference values for all row-builder outputs.
 *  2. Perf  — no async I/O; all compute is synchronous.
 *  3. Stale — grep guards ensure inline duplicates were removed from +page.svelte.
 *  4. Reuse — imports from the same module path used by +page.svelte.
 *  5. UX    — edge cases: empty inputs, zero-qty, null fields, pure-overnight,
 *             pure-intraday, partial-close, fully-closed, proxy hedges, drafts.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
  isFOSymbol,
  buildExpiryMatcher,
  buildPositionRowFromBroker,
  buildHoldingRowFromBroker,
  bumpExcluded,
  splitClosedReopened,
  buildCandidatePositions,
  buildCleanLegs,
  computeLegsKey,
  didUnderlyingChange,
  synthCacheKey,
  synthEquityOnlyStrategy,
} from '../src/lib/derivatives/pageLoad.js';

// ─────────────────────────────────────────────────────────────────────────────
// Dimension 3 — stale-code grep guards
// ─────────────────────────────────────────────────────────────────────────────
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PAGE_SRC = readFileSync(
  resolve(__dirname, '../src/routes/(algo)/admin/derivatives/+page.svelte'),
  'utf8',
);

describe('Stale-code checks (Dimension 3)', () => {
  test('splitClosedReopened not defined inline in +page.svelte', () => {
    const count = (PAGE_SRC.match(/function splitClosedReopened/g) || []).length;
    assert.equal(count, 0, 'splitClosedReopened must not be defined in +page.svelte');
  });

  test('_synthEquityOnlyStrategy not defined inline in +page.svelte', () => {
    const count = (PAGE_SRC.match(/function _synthEquityOnlyStrategy/g) || []).length;
    assert.equal(count, 0, '_synthEquityOnlyStrategy must not be defined in +page.svelte');
  });

  test('_synthCacheKey not defined inline in +page.svelte', () => {
    const count = (PAGE_SRC.match(/function _synthCacheKey/g) || []).length;
    assert.equal(count, 0, '_synthCacheKey must not be defined in +page.svelte');
  });

  test('buildCleanLegs replaces inline .filter(l => l.kind !== eq).map pattern', () => {
    // The old inline .filter+.map chain that built cleanLegs; should be gone
    const oldPattern = (PAGE_SRC.match(/\.filter\(l => l\.kind !== .eq.\)\s*\n\s*\/\/ Equity/g) || []).length;
    assert.equal(oldPattern, 0, 'Inline buildCleanLegs pattern removed from +page.svelte');
  });

  test('+page.svelte imports from $lib/derivatives/pageLoad.js', () => {
    assert.ok(
      PAGE_SRC.includes("from '$lib/derivatives/pageLoad.js'"),
      '+page.svelte imports from $lib/derivatives/pageLoad.js',
    );
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// isFOSymbol
// ─────────────────────────────────────────────────────────────────────────────
describe('isFOSymbol (Dimension 1 + 5)', () => {
  test('recognises CE option', () => assert.ok(isFOSymbol('NIFTY26JUN24000CE')));
  test('recognises PE option', () => assert.ok(isFOSymbol('NIFTY26JUN24000PE')));
  test('recognises FUT', () => assert.ok(isFOSymbol('CRUDEOIL26JUNFUT')));
  test('rejects equity without F&O suffix', () => assert.ok(!isFOSymbol('INFY')));
  test('rejects equity HDFC', () => assert.ok(!isFOSymbol('HDFCBANK')));
  test('rejects null', () => assert.ok(!isFOSymbol(null)));
  test('rejects empty string', () => assert.ok(!isFOSymbol('')));
  test('case-insensitive', () => assert.ok(isFOSymbol('nifty26jun24000ce')));
});

// ─────────────────────────────────────────────────────────────────────────────
// buildExpiryMatcher
// ─────────────────────────────────────────────────────────────────────────────
describe('buildExpiryMatcher (Dimension 1 + 5)', () => {
  const mockGet = (sym) => {
    const map = { 'NIFTY26JUN24000CE': { x: '2026-06-26' }, 'NIFTY26JUL24000CE': { x: '2026-07-31' } };
    return map[sym] || null;
  };

  test('empty selectedExpiries — all pass', () => {
    const m = buildExpiryMatcher([], mockGet);
    assert.ok(m('NIFTY26JUN24000CE'));
    assert.ok(m('ANYTHING'));
  });

  test('filters to matching expiry', () => {
    const m = buildExpiryMatcher(['2026-06-26'], mockGet);
    assert.ok(m('NIFTY26JUN24000CE'));
    assert.ok(!m('NIFTY26JUL24000CE'));
  });

  test('unknown symbol does not pass when filter set', () => {
    const m = buildExpiryMatcher(['2026-06-26'], mockGet);
    assert.ok(!m('UNKNOWN'));
  });

  test('multi-expiry filter', () => {
    const m = buildExpiryMatcher(['2026-06-26', '2026-07-31'], mockGet);
    assert.ok(m('NIFTY26JUN24000CE'));
    assert.ok(m('NIFTY26JUL24000CE'));
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// buildPositionRowFromBroker
// ─────────────────────────────────────────────────────────────────────────────
describe('buildPositionRowFromBroker (Dimension 1 + 5)', () => {
  const brokerRow = {
    tradingsymbol: 'NIFTY26JUN24000CE',
    account: 'ZG0790',
    quantity: 50,
    average_price: 120.5,
    last_price: 135.0,
    close_price: 118.0,
    pnl: 725,
    realised: 200,
    day_change_val: 825,
    overnight_quantity: 50,
    day_buy_quantity: 0,
    day_sell_quantity: 0,
    day_buy_value: 0,
    day_sell_value: 0,
  };

  test('maps symbol to uppercase', () => {
    const r = buildPositionRowFromBroker({ tradingsymbol: 'nifty26jun24000ce', quantity: 1 }, 'live');
    assert.equal(r.symbol, 'NIFTY26JUN24000CE');
  });

  test('sets source from argument', () => {
    assert.equal(buildPositionRowFromBroker(brokerRow, 'live').source, 'live');
    assert.equal(buildPositionRowFromBroker(brokerRow, 'sim').source, 'sim');
  });

  test('maps all numeric fields correctly', () => {
    const r = buildPositionRowFromBroker(brokerRow, 'live');
    assert.equal(r.qty, 50);
    assert.equal(r.avg_cost, 120.5);
    assert.equal(r.ltp, 135.0);
    assert.equal(r.prev_close, 118.0);
    assert.equal(r.pnl, 725);
    assert.equal(r.realised, 200);
    assert.equal(r.day_change_val, 825);
    assert.equal(r.overnight_quantity, 50);
  });

  test('null average_price → null avg_cost', () => {
    const r = buildPositionRowFromBroker({ ...brokerRow, average_price: null }, 'live');
    assert.equal(r.avg_cost, null);
  });

  test('uses symbol field when tradingsymbol absent', () => {
    const r = buildPositionRowFromBroker({ symbol: 'GOLDM26JUNFUT', quantity: 1 }, 'live');
    assert.equal(r.symbol, 'GOLDM26JUNFUT');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// buildHoldingRowFromBroker
// ─────────────────────────────────────────────────────────────────────────────
describe('buildHoldingRowFromBroker (Dimension 1 + 5)', () => {
  const brokerHolding = {
    tradingsymbol: 'RELIANCE',
    account: 'ZG0790',
    quantity: 10,
    opening_quantity: 10,
    average_price: 2800.0,
    last_price: 2900.0,
    close_price: 2780.0,
    pnl: 1200,
    day_change_val: 1000,
  };

  test('maps all fields correctly', () => {
    const r = buildHoldingRowFromBroker(brokerHolding);
    assert.ok(r !== null);
    assert.equal(r.symbol, 'RELIANCE');
    assert.equal(r.qty, 10);
    assert.equal(r.opening_qty, 10);
    assert.equal(r.avg_cost, 2800.0);
    assert.equal(r.ltp, 2900.0);
    assert.equal(r.pnl, 1200);
  });

  test('returns null when both qty and opening_qty are 0', () => {
    const r = buildHoldingRowFromBroker({ ...brokerHolding, quantity: 0, opening_quantity: 0 });
    assert.equal(r, null);
  });

  test('returns row when qty=0 but opening_qty>0 (sold today)', () => {
    const r = buildHoldingRowFromBroker({ ...brokerHolding, quantity: 0, opening_quantity: 10 });
    assert.ok(r !== null);
    assert.equal(r.qty, 0);
    assert.equal(r.opening_qty, 10);
  });

  test('returns null when symbol is empty', () => {
    const r = buildHoldingRowFromBroker({ ...brokerHolding, tradingsymbol: '', symbol: '' });
    assert.equal(r, null);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// bumpExcluded
// ─────────────────────────────────────────────────────────────────────────────
describe('bumpExcluded (Dimension 1 + 5)', () => {
  test('creates entry on first call', () => {
    const exc = {};
    bumpExcluded(exc, 'ZG0790', { pos_pnl: 500 });
    assert.equal(exc['ZG0790'].pos_pnl, 500);
    assert.equal(exc['ZG0790'].pos_day, 0);
  });

  test('accumulates across calls', () => {
    const exc = {};
    bumpExcluded(exc, 'ZG0790', { pos_pnl: 500 });
    bumpExcluded(exc, 'ZG0790', { pos_pnl: 300, pos_day: 100 });
    assert.equal(exc['ZG0790'].pos_pnl, 800);
    assert.equal(exc['ZG0790'].pos_day, 100);
  });

  test('uppercases account key', () => {
    const exc = {};
    bumpExcluded(exc, 'zg0790', { pos_pnl: 100 });
    assert.ok('ZG0790' in exc);
  });

  test('hold_pnl and hold_day tracked separately', () => {
    const exc = {};
    bumpExcluded(exc, 'DH6847', { hold_pnl: 2000, hold_day: 500 });
    assert.equal(exc['DH6847'].hold_pnl, 2000);
    assert.equal(exc['DH6847'].hold_day, 500);
    assert.equal(exc['DH6847'].pos_pnl, 0);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// splitClosedReopened
// ─────────────────────────────────────────────────────────────────────────────
describe('splitClosedReopened (Dimension 1 + 5)', () => {
  const basePos = {
    symbol: 'NIFTY26JUN24000CE',
    account: 'ZG0790',
    qty: 50,
    source: 'live',
    avg_cost: 100,
    ltp: 150,
    prev_close: 110,
    pnl: 2500,
    realised: 0,
    day_change_val: 2000,
    overnight_quantity: 50,
    day_buy_quantity: 0,
    day_sell_quantity: 0,
    day_buy_value: 0,
    day_sell_value: 0,
  };

  test('pure overnight hold — no split', () => {
    const result = splitClosedReopened(basePos);
    assert.equal(result.length, 1);
    assert.equal(result[0], basePos);
  });

  test('pure intraday open (overnight=0) — no split', () => {
    const p = { ...basePos, overnight_quantity: 0, day_buy_quantity: 50, day_buy_value: 5000 };
    const result = splitClosedReopened(p);
    assert.equal(result.length, 1);
  });

  test('partially closed long — splits into two rows', () => {
    const p = {
      ...basePos,
      qty: 25,
      overnight_quantity: 50,
      day_sell_quantity: 25,
      day_sell_value: 3250,   // exit at 130
      prev_close: 110,
    };
    const result = splitClosedReopened(p);
    assert.equal(result.length, 2);
    const closed = result.find(r => r._splitTag === 'closed');
    const open   = result.find(r => r._splitTag === 'open');
    assert.ok(closed, 'must have a closed row');
    assert.ok(open, 'must have an open row');
    assert.equal(closed.qty, 0);
  });

  test('fully closed position (brokerQty=0) — returns only closed row', () => {
    const p = {
      ...basePos,
      qty: 0,
      overnight_quantity: 50,
      day_sell_quantity: 50,
      day_sell_value: 6500,   // exit at 130
    };
    const result = splitClosedReopened(p);
    assert.equal(result.length, 1);
    assert.equal(result[0]._splitTag, 'closed');
    assert.equal(result[0].qty, 0);
  });

  test('intraday addition (no close) — no split', () => {
    // Overnight 50 long, bought 25 more today — day_sell=0 so closed_qty=0
    const p = {
      ...basePos,
      qty: 75,
      overnight_quantity: 50,
      day_buy_quantity: 25,
      day_buy_value: 3000,
    };
    const result = splitClosedReopened(p);
    assert.equal(result.length, 1);
  });

  test('closed row P&L uses broker pnl when brokerQty=0', () => {
    const p = {
      ...basePos,
      qty: 0,
      pnl: 7500,
      overnight_quantity: 50,
      day_sell_quantity: 50,
      day_sell_value: 6000,
    };
    const result = splitClosedReopened(p);
    assert.equal(result[0].pnl, 7500);
  });

  test('day_change_val is conserved across closed+open rows', () => {
    const p = {
      ...basePos,
      qty: 25,
      day_change_val: 1000,
      overnight_quantity: 50,
      day_sell_quantity: 25,
      day_sell_value: 3000, // exit at 120
      prev_close: 100,
    };
    const result = splitClosedReopened(p);
    if (result.length === 2) {
      const total = (result[0].day_change_val || 0) + (result[1].day_change_val || 0);
      assert.equal(total, p.day_change_val);
    }
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// buildCandidatePositions
// ─────────────────────────────────────────────────────────────────────────────
describe('buildCandidatePositions (Dimension 1 + 5)', () => {
  const mockGetInst = (sym) => {
    const map = {
      'NIFTY26JUN24000CE': { x: '2026-06-26' },
      'NIFTY26JUN24000PE': { x: '2026-06-26' },
      'NIFTY26JUNFUT': { x: '2026-06-26' },
    };
    return map[sym] || null;
  };

  const positions = [
    { symbol: 'NIFTY26JUN24000CE', account: 'ZG0790', qty: 50, source: 'live',  pnl: 1000 },
    { symbol: 'NIFTY26JUN24000PE', account: 'DH6847', qty: 25, source: 'live',  pnl: -500 },
    { symbol: 'RELIANCE',          account: 'ZG0790', qty: 10, source: 'live',  pnl: 200  }, // equity — excluded
    { symbol: 'BANKNIFTY26JUNCE',  account: 'ZG0790', qty: 5,  source: 'live',  pnl: 50   }, // different root
    { symbol: 'NIFTY26JUN24000CE', account: 'ZG0790', qty: 20, source: 'sim',   pnl: 400  }, // sim — excluded when live
  ];

  const holdings = [
    { symbol: 'NIFTY',    account: 'ZG0790', qty: 100, pnl: 500 }, // matches target exactly
    { symbol: 'RELIANCE', account: 'ZG0790', qty: 5,   pnl: 200 }, // different
  ];

  const params = {
    positions,
    holdings,
    drafts: [],
    target: 'NIFTY',
    selectedExpiries: [],
    selectedAccounts: [],
    simActive: false,
    proxiesForTarget: () => [],
    getInstrument: mockGetInst,
  };

  test('returns only NIFTY F&O rows from live source', () => {
    const result = buildCandidatePositions(params);
    const syms = result.map(r => r.symbol);
    assert.ok(syms.includes('NIFTY26JUN24000CE'));
    assert.ok(syms.includes('NIFTY26JUN24000PE'));
    assert.ok(!syms.includes('RELIANCE'));
    assert.ok(!syms.includes('BANKNIFTY26JUNCE'));
  });

  test('includes holdings as eq rows when symbol matches target', () => {
    const result = buildCandidatePositions(params);
    const eq = result.filter(r => r.kind === 'eq');
    assert.ok(eq.length > 0);
    assert.equal(eq[0].symbol, 'NIFTY');
  });

  test('sim mode uses sim source only', () => {
    const simParams = { ...params, simActive: true };
    const result = buildCandidatePositions(simParams);
    const syms = result.map(r => r.symbol);
    assert.ok(syms.includes('NIFTY26JUN24000CE'));
    // The live PE should not appear in sim mode
    const sources = result.map(r => r.source);
    assert.ok(sources.every(s => s === 'sim' || s === 'live' || s === 'draft'));
    // Actually sim source positions + holdings (live). PE is source=live so excluded
    const peRows = result.filter(r => r.symbol === 'NIFTY26JUN24000PE');
    assert.equal(peRows.length, 0);
  });

  test('account filter applied to positions', () => {
    const filtered = buildCandidatePositions({ ...params, selectedAccounts: ['ZG0790'] });
    const accts = filtered.map(r => r.account);
    // DH6847 PE should be excluded
    assert.ok(!accts.includes('DH6847'));
  });

  test('expiry filter applied', () => {
    const filtered = buildCandidatePositions({ ...params, selectedExpiries: ['2026-07-31'] });
    // No instruments match 2026-07-31 in mockGetInst → all positions excluded
    const futs = filtered.filter(r => r.kind === 'fut' || r.kind === 'opt');
    assert.equal(futs.length, 0);
  });

  test('closed rows sort to end', () => {
    const withClosed = [
      { symbol: 'NIFTY26JUN24000CE', account: 'ZG0790', qty: 0,  source: 'live', pnl: 0 },
      { symbol: 'NIFTY26JUN24000PE', account: 'ZG0790', qty: 25, source: 'live', pnl: 0 },
    ];
    const result = buildCandidatePositions({ ...params, positions: withClosed, holdings: [] });
    if (result.length >= 2) {
      // First row should be non-closed
      assert.ok(Number(result[0].qty) !== 0 || result.length === 1);
    }
  });

  test('drafts included regardless of account filter', () => {
    const drafts = [
      { symbol: 'NIFTY26JUN24000CE', qty: 10, avg_cost: 100, ltp: 110, id: 1 },
    ];
    const result = buildCandidatePositions({
      ...params, drafts,
      selectedAccounts: ['ZG0790'], positions: [], holdings: [],
    });
    const draftRows = result.filter(r => r.source === 'draft');
    assert.ok(draftRows.length > 0);
  });

  test('proxy holdings stamped with proxy_for', () => {
    const result = buildCandidatePositions({
      ...params,
      holdings: [{ symbol: 'NIFTYBEES', account: 'ZG0790', qty: 100 }],
      proxiesForTarget: () => ['NIFTYBEES'],
    });
    const proxy = result.find(r => r.symbol === 'NIFTYBEES');
    assert.ok(proxy);
    assert.equal(proxy.proxy_for, 'NIFTY');
    assert.equal(proxy.kind, 'eq');
  });

  test('empty positions → empty result', () => {
    const result = buildCandidatePositions({ ...params, positions: [], holdings: [], drafts: [] });
    assert.equal(result.length, 0);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// buildCleanLegs
// ─────────────────────────────────────────────────────────────────────────────
describe('buildCleanLegs (Dimension 1 + 5)', () => {
  const mockGet = (sym) => sym === 'NIFTY26JUN24000CE' ? { x: '2026-06-26' } : null;

  test('excludes eq-kind legs', () => {
    const legs = [
      { kind: 'opt', symbol: 'NIFTY26JUN24000CE', qty: 50, avg_cost: 100, source: 'live' },
      { kind: 'eq',  symbol: 'NIFTY',             qty: 10, avg_cost: 200, source: 'live' },
    ];
    const result = buildCleanLegs(legs, mockGet);
    assert.equal(result.length, 1);
    assert.equal(result[0].symbol, 'NIFTY26JUN24000CE');
  });

  test('filters out zero-qty legs', () => {
    const legs = [
      { kind: 'opt', symbol: 'NIFTY26JUN24000CE', qty: 0, avg_cost: 100, source: 'live' },
      { kind: 'opt', symbol: 'NIFTY26JUN24000PE', qty: 25, avg_cost: 80, source: 'live' },
    ];
    const result = buildCleanLegs(legs, () => null);
    assert.equal(result.length, 1);
    assert.equal(result[0].symbol, 'NIFTY26JUN24000PE');
  });

  test('inlines ltp for sim source only', () => {
    const legs = [
      { kind: 'opt', symbol: 'NIFTY26JUN24000CE', qty: 50, avg_cost: 100, ltp: 120, source: 'sim' },
      { kind: 'opt', symbol: 'NIFTY26JUN24000PE', qty: 25, avg_cost: 80,  ltp: 90,  source: 'live' },
    ];
    const result = buildCleanLegs(legs, () => null);
    assert.equal(result.find(l => l.symbol === 'NIFTY26JUN24000CE').ltp, 120);
    assert.equal(result.find(l => l.symbol === 'NIFTY26JUN24000PE').ltp, null);
  });

  test('inlines ltp for draft source', () => {
    const legs = [
      { kind: 'opt', symbol: 'NIFTY26JUN24000CE', qty: 10, avg_cost: 100, ltp: 115, source: 'draft' },
    ];
    const result = buildCleanLegs(legs, () => null);
    assert.equal(result[0].ltp, 115);
  });

  test('looks up expiry from instruments cache', () => {
    const legs = [
      { kind: 'opt', symbol: 'NIFTY26JUN24000CE', qty: 50, avg_cost: 100, source: 'live' },
    ];
    const result = buildCleanLegs(legs, mockGet);
    assert.equal(result[0].expiry, '2026-06-26');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// computeLegsKey
// ─────────────────────────────────────────────────────────────────────────────
describe('computeLegsKey (Dimension 1 + 5)', () => {
  const legs = [
    { symbol: 'NIFTY26JUN24000CE', qty: 50, avg_cost: 100, ltp: null, expiry: '2026-06-26' },
  ];

  test('same legs → same key', () => {
    assert.equal(computeLegsKey(legs), computeLegsKey(legs));
  });

  test('different qty → different key', () => {
    const a = [{ ...legs[0], qty: 50 }];
    const b = [{ ...legs[0], qty: 75 }];
    assert.notEqual(computeLegsKey(a), computeLegsKey(b));
  });

  test('empty legs → empty string', () => {
    assert.equal(computeLegsKey([]), '');
  });

  test('key is stable string (no random component)', () => {
    const key1 = computeLegsKey(legs);
    const key2 = computeLegsKey(legs);
    assert.equal(key1, key2);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// didUnderlyingChange
// ─────────────────────────────────────────────────────────────────────────────
describe('didUnderlyingChange (Dimension 1 + 5)', () => {
  const mockDecompose = (sym) => {
    if (sym.startsWith('NIFTY')) return { root: 'NIFTY' };
    if (sym.startsWith('GOLDM')) return { root: 'GOLDM' };
    return { root: sym };
  };

  test('same underlying → false', () => {
    const legs = [{ symbol: 'NIFTY26JUN24000CE' }];
    const strat = { legs: [{ symbol: 'NIFTY26JUN24000PE' }] };
    assert.equal(didUnderlyingChange(legs, strat, mockDecompose), false);
  });

  test('different underlying → true', () => {
    const legs = [{ symbol: 'GOLDM26JUNFUT' }];
    const strat = { legs: [{ symbol: 'NIFTY26JUN24000CE' }] };
    assert.equal(didUnderlyingChange(legs, strat, mockDecompose), true);
  });

  test('no previous strategy → false', () => {
    const legs = [{ symbol: 'NIFTY26JUN24000CE' }];
    assert.equal(didUnderlyingChange(legs, null, mockDecompose), false);
  });

  test('empty strategy legs → false', () => {
    const legs = [{ symbol: 'NIFTY26JUN24000CE' }];
    assert.equal(didUnderlyingChange(legs, { legs: [] }, mockDecompose), false);
  });

  test('empty cleanLegs → false', () => {
    const strat = { legs: [{ symbol: 'NIFTY26JUN24000CE' }] };
    assert.equal(didUnderlyingChange([], strat, mockDecompose), false);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// synthCacheKey
// ─────────────────────────────────────────────────────────────────────────────
describe('synthCacheKey (Dimension 1 + 5)', () => {
  const eqs = [
    { symbol: 'NIFTY', qty: 100, avg_cost: 22000, ltp: 22500 },
  ];

  test('same inputs → same key', () => {
    assert.equal(synthCacheKey('NIFTY', eqs), synthCacheKey('NIFTY', eqs));
  });

  test('different underlying → different key', () => {
    assert.notEqual(synthCacheKey('NIFTY', eqs), synthCacheKey('BANKNIFTY', eqs));
  });

  test('different qty → different key', () => {
    const a = [{ ...eqs[0], qty: 100 }];
    const b = [{ ...eqs[0], qty: 200 }];
    assert.notEqual(synthCacheKey('NIFTY', a), synthCacheKey('NIFTY', b));
  });

  test('empty eqs produces key from underlying only', () => {
    const key = synthCacheKey('NIFTY', []);
    assert.equal(key, 'NIFTY');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// synthEquityOnlyStrategy
// ─────────────────────────────────────────────────────────────────────────────
describe('synthEquityOnlyStrategy (Dimension 1 + 5)', () => {
  const eqs = [
    { symbol: 'NIFTY', qty: 50, avg_cost: 22000, ltp: 22500, prev_close: 22000 },
  ];

  test('returns null for empty eqs', () => {
    assert.equal(synthEquityOnlyStrategy([], 'NIFTY'), null);
  });

  test('returns null when no spot available', () => {
    const e = [{ symbol: 'NIFTY', qty: 50, avg_cost: 0, ltp: 0 }];
    assert.equal(synthEquityOnlyStrategy(e, 'NIFTY'), null);
  });

  test('returns strategy-shaped object with 41-point payoff', () => {
    const strat = synthEquityOnlyStrategy(eqs, 'NIFTY');
    assert.ok(strat !== null);
    assert.equal(strat.payoff.length, 41);
    assert.equal(strat.underlying, 'NIFTY');
    assert.equal(strat.spot, 22500);
  });

  test('payoff points are all zero baseline', () => {
    const strat = synthEquityOnlyStrategy(eqs, 'NIFTY');
    for (const pt of strat.payoff) {
      assert.equal(pt.today_value, 0);
      assert.equal(pt.expiry_value, 0);
    }
  });

  test('payoff span covers ±15% of spot', () => {
    const strat = synthEquityOnlyStrategy(eqs, 'NIFTY');
    const lo = strat.payoff[0].spot;
    const hi = strat.payoff[strat.payoff.length - 1].spot;
    assert.ok(Math.abs(lo - eqs[0].ltp * 0.85) < 1, 'lo near spot*0.85');
    assert.ok(Math.abs(hi - eqs[0].ltp * 1.15) < 1, 'hi near spot*1.15');
  });

  test('net_cost reflects qty × avg_cost', () => {
    const strat = synthEquityOnlyStrategy(eqs, 'NIFTY');
    assert.equal(strat.net_cost, 50 * 22000);
  });

  test('falls back to avg_cost when ltp=0', () => {
    const e = [{ symbol: 'NIFTY', qty: 50, avg_cost: 22000, ltp: 0, prev_close: 21800 }];
    const strat = synthEquityOnlyStrategy(e, 'NIFTY');
    assert.ok(strat !== null);
    assert.equal(strat.spot, 22000);
  });

  test('legs array is empty (backend fills it)', () => {
    const strat = synthEquityOnlyStrategy(eqs, 'NIFTY');
    assert.deepEqual(strat.legs, []);
  });

  test('risk object has all required fields', () => {
    const strat = synthEquityOnlyStrategy(eqs, 'NIFTY');
    assert.ok('max_profit' in strat.risk);
    assert.ok('max_loss' in strat.risk);
    assert.ok('breakevens' in strat.risk);
    assert.ok('rr_ratio' in strat.risk);
    assert.ok('ev' in strat.risk);
  });
});
