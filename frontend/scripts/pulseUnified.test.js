/**
 * pulseUnified.test.js — Unit tests for the MarketPulse buildUnified helpers.
 *
 * Five quality dimensions (per project test convention):
 *  1. SSOT   — golden inputs → golden outputs; math correctness
 *  2. Perf   — all functions are sync-only, no I/O
 *  3. Stale  — grep guard: no duplicate section logic in MarketPulse.svelte
 *  4. Reuse  — MarketPulse.svelte imports from pulseUnified (grep guard)
 *  5. UX     — edge cases: empty inputs, null/missing fields, direction encoding
 *
 * Run with:  node --test frontend/scripts/pulseUnified.test.js
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import {
  parseSymbolFallback,
  parseSymbol,
  fillSymbolMeta,
  makeRowFactory,
  mergeWatchlistRows,
  mergePositionRows,
  mergeHoldingRows,
  mergeUnderlyingAnchors,
  mergeMoverRows,
  tagWatchedIndices,
  finalizeRows,
  sortUnifiedRows,
  MAJOR_ORDER,
  MAJOR_SUFFIX,
  INDEX_TO_UNDERLYING,
} from '../src/lib/data/pulseUnified.js';

import {
  baseDayPnlForPosition,
  livePositionDayPnl,
} from '../src/lib/data/nav.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND_SRC = resolve(__dirname, '..', 'src');

// ── Helpers ──────────────────────────────────────────────────────────────────

/** No-op context for helpers that need snapOf / isMarketOpen etc. */
function noSnapCtx() {
  return {
    snapOf: () => null,
    getInst: null,
    isMarketOpen: () => true,
    baseDayPnlForPosition: (r) => Number(r.day_change_val) || 0,
    directional: (changePct) => changePct,
    leadAccount: (row) => row.accounts && row.accounts.size > 0 ? [...row.accounts][0] : null,
    acctColor: () => '#aabbcc',
  };
}

function makeByKey() {
  return /** @type {Record<string, any>} */ ({});
}

// ── 1. SSOT: parseSymbolFallback ─────────────────────────────────────────────

describe('parseSymbolFallback', () => {
  test('parses NSE CE option', () => {
    const r = parseSymbolFallback('NIFTY25APR21500CE');
    assert.equal(r.underlying, 'NIFTY');
    assert.equal(r.kind,       'opt');
    assert.equal(r.strike,     21500);
    assert.equal(r.opt_type,   'CE');
    assert.equal(r.expiry,     '25APR');
  });

  test('parses NSE PE option', () => {
    const r = parseSymbolFallback('NIFTY25APR21500PE');
    assert.equal(r.opt_type, 'PE');
    assert.equal(r.kind,     'opt');
  });

  test('parses future', () => {
    const r = parseSymbolFallback('NIFTY25APRFUT');
    assert.equal(r.underlying, 'NIFTY');
    assert.equal(r.kind,       'fut');
    assert.equal(r.opt_type,   null);
    assert.equal(r.strike,     null);
  });

  test('parses MCX CE option', () => {
    const r = parseSymbolFallback('CRUDEOIL26JUN10000CE');
    assert.equal(r.underlying, 'CRUDEOIL');
    assert.equal(r.strike,     10000);
    assert.equal(r.kind,       'opt');
  });

  test('returns empty object for equity symbol', () => {
    const r = parseSymbolFallback('RELIANCE');
    assert.equal(r.underlying, null);
    assert.equal(r.kind,       null);
  });

  test('returns empty for empty string', () => {
    const r = parseSymbolFallback('');
    assert.equal(r.underlying, null);
  });
});

// ── 2. SSOT: parseSymbol delegates to fallback when no cache ─────────────────

describe('parseSymbol', () => {
  test('uses getInst when available', () => {
    const getInst = (sym) => sym === 'NIFTY25APR21500CE'
      ? { t: 'CE', k: 21500, u: 'NIFTY', x: '25APR' } : null;
    const r = parseSymbol('NIFTY25APR21500CE', getInst);
    assert.equal(r.underlying, 'NIFTY');
    assert.equal(r.kind,       'opt');
    assert.equal(r.strike,     21500);
    assert.equal(r.opt_type,   'CE');
    assert.equal(r.expiry,     '25APR');
  });

  test('falls back to regex when getInst returns null', () => {
    const r = parseSymbol('GOLDM26JUN78000CE', () => null);
    assert.equal(r.underlying, 'GOLDM');
    assert.equal(r.kind,       'opt');
    assert.equal(r.strike,     78000);
  });

  test('handles getInst = null', () => {
    const r = parseSymbol('NIFTY25APRFUT', null);
    assert.equal(r.kind, 'fut');
  });
});

// ── 3. SSOT: makeRowFactory ───────────────────────────────────────────────────

describe('makeRowFactory', () => {
  test('creates and deduplicates rows per sym+major', () => {
    const byKey = makeByKey();
    const get = makeRowFactory(byKey);
    const r1 = get('NIFTY 50', 'pinned');
    const r2 = get('NIFTY 50', 'pinned');
    assert.equal(r1, r2, 'same key returns same object');
    assert.equal(r1._majorGroup, 'pinned');
    assert.equal(r1._majorOrder, MAJOR_ORDER.pinned);
    assert.equal(Object.keys(byKey).length, 1);
  });

  test('creates separate row for different majors', () => {
    const byKey = makeByKey();
    const get = makeRowFactory(byKey);
    const r1 = get('RELIANCE', 'watchlist');
    const r2 = get('RELIANCE', 'positions');
    assert.notEqual(r1, r2);
    assert.equal(Object.keys(byKey).length, 2);
    assert.equal(r1.key, `RELIANCE${MAJOR_SUFFIX.watchlist}`);
    assert.equal(r2.key, `RELIANCE${MAJOR_SUFFIX.positions}`);
  });

  test('initial row has zero qty and null pnl', () => {
    const byKey = makeByKey();
    const get = makeRowFactory(byKey);
    const row = get('INFY', 'holdings');
    assert.equal(row.qty_pos,  0);
    assert.equal(row.qty_hold, 0);
    assert.equal(row.pnl,      null);
    assert.equal(row.day_pnl,  null);
    assert.ok(row.accounts instanceof Set);
  });
});

// ── 4. SSOT: mergeWatchlistRows ───────────────────────────────────────────────

describe('mergeWatchlistRows', () => {
  test('pinned list creates pinned-major rows', () => {
    const byKey = makeByKey();
    const actLists = [{
      id: 1, is_pinned: true,
      items: [{ tradingsymbol: 'NIFTY 50', exchange: 'NSE', id: 10, alias: null }],
    }];
    const ctx = { snapOf: () => null, getInst: null };
    mergeWatchlistRows(byKey, actLists, ctx);
    const key = `NIFTY 50${MAJOR_SUFFIX.pinned}`;
    assert.ok(byKey[key], 'pinned row created');
    assert.equal(byKey[key]._majorGroup, 'pinned');
    assert.equal(byKey[key].src.w, true);
    assert.equal(byKey[key]._fromPinnedList, true);
  });

  test('non-pinned list creates watchlist-major rows', () => {
    const byKey = makeByKey();
    const actLists = [{
      id: 2, is_pinned: false,
      items: [{ tradingsymbol: 'RELIANCE', exchange: 'NSE', id: 20, alias: null }],
    }];
    mergeWatchlistRows(byKey, actLists, { snapOf: () => null, getInst: null });
    const key = `RELIANCE${MAJOR_SUFFIX.watchlist}`;
    assert.ok(byKey[key]);
    assert.equal(byKey[key]._fromPinnedList, undefined);
  });

  test('alias sets display_name', () => {
    const byKey = makeByKey();
    const actLists = [{
      id: 3, is_pinned: false,
      items: [{ tradingsymbol: 'CRUDEOIL26JUNFUT', exchange: 'MCX', id: 30, alias: 'Crude oil' }],
    }];
    mergeWatchlistRows(byKey, actLists, { snapOf: () => null, getInst: null });
    const key = `CRUDEOIL26JUNFUT${MAJOR_SUFFIX.watchlist}`;
    assert.equal(byKey[key].display_name, 'Crude oil');
  });

  test('snap values land on the row', () => {
    const byKey = makeByKey();
    const actLists = [{
      id: 4, is_pinned: true,
      items: [{ tradingsymbol: 'BANKNIFTY', exchange: 'NSE', id: 40, alias: null }],
    }];
    const snapOf = (sym) => sym === 'BANKNIFTY'
      ? { ltp: 52000, close: 51000, day_change: 1000, day_change_pct: 1.96, volume: 500000 }
      : null;
    mergeWatchlistRows(byKey, actLists, { snapOf, getInst: null });
    const row = byKey[`BANKNIFTY${MAJOR_SUFFIX.pinned}`];
    assert.equal(row.ltp,    52000);
    assert.equal(row.close,  51000);
    assert.equal(row.change, 1000);
    assert.equal(row.volume, 500000);
  });

  test('skips empty tradingsymbol', () => {
    const byKey = makeByKey();
    const actLists = [{
      id: 5, is_pinned: false,
      items: [{ tradingsymbol: '', exchange: 'NSE', id: 50, alias: null }],
    }];
    mergeWatchlistRows(byKey, actLists, { snapOf: () => null, getInst: null });
    assert.equal(Object.keys(byKey).length, 0);
  });
});

// ── 5. SSOT: mergePositionRows — Day P&L math ─────────────────────────────────

describe('mergePositionRows', () => {
  test('basic position row created', () => {
    const byKey = makeByKey();
    const pos = [{
      tradingsymbol: 'NIFTY25APR21500CE',
      exchange: 'NFO',
      quantity: 50,
      average_price: 200,
      pnl: 500,
      day_change_val: 250,
      last_price: 210,
      close_price: 205,
      account: 'ZG0790',
    }];
    const ctx = noSnapCtx();
    mergePositionRows(byKey, pos, true, {}, ctx);
    const key = `NIFTY25APR21500CE${MAJOR_SUFFIX.positions}`;
    assert.ok(byKey[key]);
    const row = byKey[key];
    assert.equal(row.qty_pos, 50);
    assert.equal(row.src.p,   true);
    assert.ok(row.accounts.has('ZG0790'));
  });

  test('multi-account merge accumulates qty and avg', () => {
    const byKey = makeByKey();
    const pos = [
      { tradingsymbol: 'RELIANCE', exchange: 'NSE', quantity: 10, average_price: 2000, pnl: 100, day_change_val: 50, last_price: 2010, close_price: 2005, account: 'ZG0790' },
      { tradingsymbol: 'RELIANCE', exchange: 'NSE', quantity: 5,  average_price: 2020, pnl: 40,  day_change_val: 20, last_price: 2010, close_price: 2005, account: 'DH3747' },
    ];
    const ctx = noSnapCtx();
    mergePositionRows(byKey, pos, true, {}, ctx);
    const key = `RELIANCE${MAJOR_SUFFIX.positions}`;
    const row = byKey[key];
    assert.equal(row.qty_pos, 15);  // 10 + 5
    assert.ok(row.accounts.has('ZG0790'));
    assert.ok(row.accounts.has('DH3747'));
  });

  test('respects includePos=false', () => {
    const byKey = makeByKey();
    const pos = [{ tradingsymbol: 'INFY', exchange: 'NSE', quantity: 10, average_price: 1500, pnl: 100, day_change_val: 50, last_price: 1510, close_price: 1505 }];
    mergePositionRows(byKey, pos, false, {}, noSnapCtx());
    assert.equal(Object.keys(byKey).length, 0);
  });

  test('broker raw values mirrored on row', () => {
    const byKey = makeByKey();
    const pos = [{
      tradingsymbol: 'NIFTY25APRFUT',
      exchange: 'NFO',
      quantity: 75,
      average_price: 22000,
      pnl: 1500,
      day_change_val: 750,
      last_price: 22020,
      close_price: 22010,
    }];
    mergePositionRows(byKey, pos, true, {}, noSnapCtx());
    const key = `NIFTY25APRFUT${MAJOR_SUFFIX.positions}`;
    const row = byKey[key];
    assert.equal(row._broker_pnl,     1500);
    assert.equal(row._broker_day_pnl, 750);   // baseDayPnlForPosition returns day_change_val
  });

  test('account_stale propagation', () => {
    const byKey = makeByKey();
    const pos = [{
      tradingsymbol: 'GOLDM26JUNFUT',
      exchange: 'MCX',
      quantity: 1,
      average_price: 78000,
      pnl: 0,
      day_change_val: 0,
      last_price: 78000,
      close_price: 77900,
      account_stale: true,
      account_stale_since: '2026-07-04T09:00:00Z',
    }];
    mergePositionRows(byKey, pos, true, {}, noSnapCtx());
    const key = `GOLDM26JUNFUT${MAJOR_SUFFIX.positions}`;
    assert.equal(byKey[key].account_stale, true);
    assert.equal(byKey[key].account_stale_since, '2026-07-04T09:00:00Z');
  });
});

// ── 6. SSOT: mergeHoldingRows ─────────────────────────────────────────────────

describe('mergeHoldingRows', () => {
  test('basic holding row created', () => {
    const byKey = makeByKey();
    const hold = [{
      tradingsymbol: 'INFY',
      exchange: 'NSE',
      opening_quantity: 100,
      average_price: 1400,
      pnl: 3000,
      day_change_val: 150,
      last_price: 1430,
      close_price: 1428,
      account: 'ZG0790',
    }];
    mergeHoldingRows(byKey, hold, true, {}, noSnapCtx());
    const key = `INFY${MAJOR_SUFFIX.holdings}`;
    const row = byKey[key];
    assert.ok(row);
    assert.equal(row.qty_hold, 100);
    assert.equal(row.src.h,   true);
    assert.ok(row.accounts.has('ZG0790'));
  });

  test('respects includeHold=false', () => {
    const byKey = makeByKey();
    const hold = [{ tradingsymbol: 'TCS', opening_quantity: 50, average_price: 3000, pnl: 1000, day_change_val: 100 }];
    mergeHoldingRows(byKey, hold, false, {}, noSnapCtx());
    assert.equal(Object.keys(byKey).length, 0);
  });

  test('close fallback from r.close_price when snap absent', () => {
    const byKey = makeByKey();
    const hold = [{
      tradingsymbol: 'WIPRO',
      exchange: 'NSE',
      opening_quantity: 200,
      average_price: 400,
      pnl: 0,
      day_change_val: 0,
      last_price: 402,
      close_price: 401,
    }];
    mergeHoldingRows(byKey, hold, true, {}, noSnapCtx());
    const key = `WIPRO${MAJOR_SUFFIX.holdings}`;
    assert.equal(byKey[key].close, 401);
  });
});

// ── 7. SSOT: mergeUnderlyingAnchors ──────────────────────────────────────────

describe('mergeUnderlyingAnchors', () => {
  test('anchor row created for option underlying', () => {
    const byKey = makeByKey();
    const uq = {
      NIFTY: {
        _resolved: { tradingsymbol: 'NIFTY 50', exchange: 'NSE', kind: 'spot', _major: 'positions', displayUnderlying: 'NIFTY', underlying_group: 'NIFTY' },
        ltp: 22000, bid: 21990, ask: 22010, close: 21900, change: 100, change_pct: 0.46, open: 21950, high: 22100, low: 21880, volume: null, oi: null,
      },
    };
    const pos = [{ tradingsymbol: 'NIFTY25APR21500CE', symbol: '', quantity: 50, average_price: 100 }];
    const ctx = { getInst: null };
    mergeUnderlyingAnchors(byKey, uq, pos, [], true, true, ctx);
    const key = `NIFTY 50${MAJOR_SUFFIX.positions}`;
    assert.ok(byKey[key], 'anchor row created');
    assert.equal(byKey[key].ltp,   22000);
    assert.equal(byKey[key].close, 21900);
    assert.equal(byKey[key].src.u, true);
  });

  test('anchor skipped when no CE/PE in scoped positions', () => {
    const byKey = makeByKey();
    const uq = {
      NIFTY: {
        _resolved: { tradingsymbol: 'NIFTY 50', exchange: 'NSE', kind: 'spot', _major: 'positions', displayUnderlying: 'NIFTY', underlying_group: 'NIFTY' },
        ltp: 22000,
      },
    };
    // pos has only a FUT, no CE/PE
    const pos = [{ tradingsymbol: 'NIFTY25APRFUT', symbol: '', quantity: 75, average_price: 22000 }];
    mergeUnderlyingAnchors(byKey, uq, pos, [], true, true, { getInst: null });
    assert.equal(Object.keys(byKey).length, 0, 'no anchor when no options');
  });

  test('anchor skipped when includePos=false', () => {
    const byKey = makeByKey();
    const uq = {
      NIFTY: {
        _resolved: { tradingsymbol: 'NIFTY 50', exchange: 'NSE', kind: 'spot', _major: 'positions', displayUnderlying: 'NIFTY', underlying_group: 'NIFTY' },
        ltp: 22000,
      },
    };
    const pos = [{ tradingsymbol: 'NIFTY25APR21500CE', quantity: 50, average_price: 100 }];
    mergeUnderlyingAnchors(byKey, uq, pos, [], false, true, { getInst: null });
    assert.equal(Object.keys(byKey).length, 0);
  });
});

// ── 8. SSOT: mergeMoverRows ───────────────────────────────────────────────────

describe('mergeMoverRows', () => {
  test('creates movers-major row', () => {
    const byKey = makeByKey();
    const movers = [{ tradingsymbol: 'RELIANCE', exchange: 'NSE', last_price: 2900, change_pct: 2.5, previous_close: 2829, sticky: false }];
    mergeMoverRows(byKey, movers, true, true, true, true, { snapOf: () => null });
    const key = `RELIANCE${MAJOR_SUFFIX.movers}`;
    assert.ok(byKey[key]);
    assert.equal(byKey[key].src.m, true);
    assert.equal(byKey[key]._majorGroup, 'movers');
  });

  test('badges existing row with src.m when symbol already present', () => {
    const byKey = makeByKey();
    // Pre-seed a watchlist row for RELIANCE
    const get = makeRowFactory(byKey);
    const existingRow = get('RELIANCE', 'watchlist');
    existingRow.tradingsymbol = 'RELIANCE';
    const movers = [{ tradingsymbol: 'RELIANCE', exchange: 'NSE', last_price: 2900, change_pct: 2.5, sticky: true }];
    mergeMoverRows(byKey, movers, true, true, true, true, { snapOf: () => null });
    assert.equal(existingRow.src.m, true, 'existing row badged');
    assert.equal(existingRow._mover_sticky, true);
    // Dedicated movers row also created
    const movKey = `RELIANCE${MAJOR_SUFFIX.movers}`;
    assert.ok(byKey[movKey], 'dedicated movers row also created');
  });

  test('strips movers rows when includeMovers=false', () => {
    const byKey = makeByKey();
    const movers = [{ tradingsymbol: 'INFY', exchange: 'NSE', last_price: 1500, change_pct: 1.2 }];
    mergeMoverRows(byKey, movers, false, true, true, true, { snapOf: () => null });
    const key = `INFY${MAJOR_SUFFIX.movers}`;
    assert.equal(byKey[key], undefined, 'movers row stripped');
  });

  test('mover row close computed from ltp - previous_close', () => {
    const byKey = makeByKey();
    const movers = [{ tradingsymbol: 'HDFC', exchange: 'NSE', last_price: 1600, change_pct: 1.0, previous_close: 1584 }];
    mergeMoverRows(byKey, movers, true, true, true, true, { snapOf: () => null });
    const key = `HDFC${MAJOR_SUFFIX.movers}`;
    const row = byKey[key];
    assert.equal(row.ltp,   1600);
    assert.equal(row.close, 1584);
    assert.equal(row.change, 1600 - 1584);
  });

  test('mover group tags propagated', () => {
    const byKey = makeByKey();
    const movers = [{ tradingsymbol: 'TCS', exchange: 'NSE', _moverGroups: ['large_cap', 'winners'], _moverGroup: 'large_cap', _moverDirection: 'up', change_pct: 1.5 }];
    mergeMoverRows(byKey, movers, true, true, true, true, { snapOf: () => null });
    const row = byKey[`TCS${MAJOR_SUFFIX.movers}`];
    assert.deepEqual(row._moverGroups,    ['large_cap', 'winners']);
    assert.equal(row._moverGroup,         'large_cap');
    assert.equal(row._moverDirection,     'up');
  });
});

// ── 9. SSOT: tagWatchedIndices ────────────────────────────────────────────────

describe('tagWatchedIndices', () => {
  test('NIFTY 50 tagged as NIFTY underlying spot', () => {
    const byKey = makeByKey();
    const get = makeRowFactory(byKey);
    const row = get('NIFTY 50', 'pinned');
    row.tradingsymbol = 'NIFTY 50';
    tagWatchedIndices(byKey);
    assert.equal(row.underlying, 'NIFTY');
    assert.equal(row.kind,       'spot');
  });

  test('INDIA VIX tagged as INDIAVIX', () => {
    const byKey = makeByKey();
    const get = makeRowFactory(byKey);
    const row = get('INDIA VIX', 'pinned');
    row.tradingsymbol = 'INDIA VIX';
    tagWatchedIndices(byKey);
    assert.equal(row.underlying, 'INDIAVIX');
  });

  test('equity symbol not in map left unchanged', () => {
    const byKey = makeByKey();
    const get = makeRowFactory(byKey);
    const row = get('RELIANCE', 'watchlist');
    row.tradingsymbol = 'RELIANCE';
    row.underlying = null;
    tagWatchedIndices(byKey);
    assert.equal(row.underlying, null, 'equity unchanged');
  });
});

// ── 10. SSOT: finalizeRows ────────────────────────────────────────────────────

describe('finalizeRows', () => {
  test('weighted avg_pos computed correctly', () => {
    const byKey = makeByKey();
    const get = makeRowFactory(byKey);
    const row = get('NIFTY25APR21500CE', 'positions');
    row.tradingsymbol = 'NIFTY25APR21500CE';
    // Two fills: 50 @ 200 + 25 @ 180
    row.qty_pos   = 75;
    row._avg_num  = 50 * 200 + 25 * 180;  // = 14500
    row._avg_hold_num = 0;
    row.qty_hold  = 0;
    row.change_pct = 1.5;
    row.ltp       = 210;
    row.close     = 205;
    const ctx = {
      directional: (cp) => cp,
      leadAccount: () => null,
      acctColor: () => null,
    };
    finalizeRows(byKey, ctx);
    // avg_pos = 14500 / 75 = 193.33...
    assert.ok(Math.abs(row.avg_pos - 193.333) < 0.01);
    assert.equal(row.avg_hold, undefined, 'no avg_hold for pure position row');
    // _avg_num and _avg_hold_num cleaned up
    assert.equal(row._avg_num,      undefined);
    assert.equal(row._avg_hold_num, undefined);
  });

  test('inv_val and cur_val for holdings', () => {
    const byKey = makeByKey();
    const get = makeRowFactory(byKey);
    const row = get('TCS', 'holdings');
    row.tradingsymbol = 'TCS';
    row.qty_hold      = 100;
    row._avg_hold_num = 100 * 3600;   // cost basis = 360000
    row._avg_num      = 0;
    row.qty_pos       = 0;
    row.ltp           = 3700;
    row.close         = 3680;
    row.change_pct    = 0.54;
    finalizeRows(byKey, {
      directional: (cp) => cp,
      leadAccount: () => null,
      acctColor: () => null,
    });
    assert.equal(row.inv_val, 360000);
    assert.equal(row.cur_val, 100 * 3700);
  });

  test('_prev_market_value from close × qty', () => {
    const byKey = makeByKey();
    const get = makeRowFactory(byKey);
    const row = get('INFY', 'holdings');
    row.tradingsymbol = 'INFY';
    row.qty_hold      = 50;
    row._avg_hold_num = 50 * 1500;
    row._avg_num      = 0;
    row.qty_pos       = 0;
    row.ltp           = 1550;
    row.close         = 1520;
    row.change_pct    = 1.97;
    finalizeRows(byKey, {
      directional: (cp) => cp,
      leadAccount: () => null,
      acctColor: () => null,
    });
    assert.equal(row._prev_market_value, 50 * 1520);
  });

  test('_acctColor assigned via ctx helpers', () => {
    const byKey = makeByKey();
    const get = makeRowFactory(byKey);
    const row = get('RELIANCE', 'positions');
    row.tradingsymbol = 'RELIANCE';
    row.accounts = new Set(['ZG0790']);
    row._avg_num = 0; row._avg_hold_num = 0; row.qty_pos = 0; row.qty_hold = 0;
    row.ltp = 2900; row.close = 2850;
    finalizeRows(byKey, {
      directional: () => null,
      leadAccount: (r) => [...r.accounts][0],
      acctColor: (acct) => acct === 'ZG0790' ? '#aabbcc' : null,
    });
    assert.equal(row._acctColor, '#aabbcc');
  });
});

// ── 11. SSOT: sortUnifiedRows ─────────────────────────────────────────────────

describe('sortUnifiedRows', () => {
  function makeRow(sym, major, underlying, kind, strike, opt_type, src) {
    return {
      tradingsymbol: sym,
      _majorGroup: major,
      underlying, kind, strike, opt_type,
      src: src || { w: false, p: false, h: false },
    };
  }

  test('watchlist bucket before positions bucket', () => {
    const rows = [
      makeRow('NIFTY25APR21500CE', 'positions', 'NIFTY', 'opt', 21500, 'CE', { w: false, p: true, h: false }),
      makeRow('NIFTY 50', 'watchlist', 'NIFTY', 'spot', null, null, { w: true, p: false, h: false }),
    ];
    const sorted = sortUnifiedRows(rows, {}, []);
    assert.equal(sorted[0].tradingsymbol, 'NIFTY 50');     // watchlist (bucket 1) before positions (bucket 2)
    assert.equal(sorted[1].tradingsymbol, 'NIFTY25APR21500CE');
  });

  test('within group: spot before fut before opt', () => {
    const rows = [
      makeRow('NIFTY25APR21500CE', 'positions', 'NIFTY', 'opt',  21500, 'CE', { w: false, p: true, h: false }),
      makeRow('NIFTY25APRFUT',     'positions', 'NIFTY', 'fut',  null,  null, { w: false, p: true, h: false }),
      makeRow('NIFTY 50',          'positions', 'NIFTY', 'spot', null,  null, { w: false, p: true, h: false }),
    ];
    const sorted = sortUnifiedRows(rows, {}, []);
    assert.equal(sorted[0].tradingsymbol, 'NIFTY 50');      // spot = 0
    assert.equal(sorted[1].tradingsymbol, 'NIFTY25APRFUT'); // fut = 1
    assert.equal(sorted[2].tradingsymbol, 'NIFTY25APR21500CE'); // opt = 2
  });

  test('options sort by strike ASC, CE before PE', () => {
    const rows = [
      makeRow('NIFTY25APR21500PE', 'positions', 'NIFTY', 'opt', 21500, 'PE', { w: false, p: true }),
      makeRow('NIFTY25APR21000CE', 'positions', 'NIFTY', 'opt', 21000, 'CE', { w: false, p: true }),
      makeRow('NIFTY25APR21500CE', 'positions', 'NIFTY', 'opt', 21500, 'CE', { w: false, p: true }),
    ];
    const sorted = sortUnifiedRows(rows, {}, []);
    assert.equal(sorted[0].tradingsymbol, 'NIFTY25APR21000CE');
    assert.equal(sorted[1].tradingsymbol, 'NIFTY25APR21500CE');
    assert.equal(sorted[2].tradingsymbol, 'NIFTY25APR21500PE');
  });

  test('manual groupOrder overrides alpha sort', () => {
    const rows = [
      makeRow('BANKNIFTY25APR48000CE', 'positions', 'BANKNIFTY', 'opt', 48000, 'CE', { w: false, p: true }),
      makeRow('NIFTY25APR21500CE',     'positions', 'NIFTY',     'opt', 21500, 'CE', { w: false, p: true }),
    ];
    // Manually rank BANKNIFTY before NIFTY
    const groupOrder = { BANKNIFTY: 0, NIFTY: 1 };
    const sorted = sortUnifiedRows(rows, groupOrder, []);
    assert.equal(sorted[0].underlying, 'BANKNIFTY');
    assert.equal(sorted[1].underlying, 'NIFTY');
  });

  test('detached symbols get __DETACHED__ groupKey prefix', () => {
    const rows = [
      makeRow('WIPRO',    'watchlist', null, null, null, null, { w: true }),
      makeRow('RELIANCE', 'watchlist', null, null, null, null, { w: true }),
    ];
    const sorted = sortUnifiedRows(rows, {}, ['RELIANCE']);
    // Both rows are in the same watchlist bucket.
    // WIPRO    → groupKey: "~~WIPRO"         (no underlying)
    // RELIANCE → groupKey: "__DETACHED__RELIANCE" (detached)
    // "__" (ASCII 95) sorts before "~~" (ASCII 126) via localeCompare,
    // so RELIANCE actually sorts BEFORE WIPRO.
    assert.equal(sorted[0].tradingsymbol, 'RELIANCE');
    assert.equal(sorted[1].tradingsymbol, 'WIPRO');
  });
});

// ── 12. Perf: all functions are sync (no Promise returns) ─────────────────────

describe('Perf: sync-only', () => {
  test('mergeWatchlistRows is sync', () => {
    const byKey = makeByKey();
    const result = mergeWatchlistRows(byKey, [], { snapOf: () => null, getInst: null });
    assert.equal(result, undefined, 'returns undefined (sync)');
    assert.notEqual(result instanceof Promise, true);
  });

  test('sortUnifiedRows is sync', () => {
    const result = sortUnifiedRows([], {}, []);
    assert.ok(Array.isArray(result));
  });
});

// ── 13. Stale: section logic not duplicated in MarketPulse.svelte ─────────────

describe('Stale: no duplicate section logic in MarketPulse.svelte', () => {
  const svelteSrc = readFileSync(
    resolve(FRONTEND_SRC, 'lib', 'MarketPulse.svelte'), 'utf-8'
  );

  test('MAJOR_SUFFIX constant not re-declared in MarketPulse.svelte', () => {
    const count = (svelteSrc.match(/MAJOR_SUFFIX\s*=/g) || []).length;
    assert.equal(count, 0, 'MAJOR_SUFFIX should only live in pulseUnified.js');
  });

  test('INDEX_TO_UNDERLYING map not re-declared in MarketPulse.svelte', () => {
    const count = (svelteSrc.match(/INDEX_TO_UNDERLYING\s*=/g) || []).length;
    assert.equal(count, 0, 'INDEX_TO_UNDERLYING should only live in pulseUnified.js');
  });

  test('buildUnified body is thin — no inline "qty_pos +=" accumulation', () => {
    // The heavy qty accumulation loop was in the old buildUnified body.
    // After decomposition, qty_pos += only lives in mergePositionRows helper.
    const buildStart = svelteSrc.indexOf('function buildUnified(');
    const buildEnd   = svelteSrc.indexOf('\n  }', buildStart) + 3;
    const buildBody  = svelteSrc.slice(buildStart, buildEnd);
    assert.equal(
      (buildBody.match(/qty_pos\s*\+=/g) || []).length, 0,
      'qty_pos += should not appear in buildUnified body — moved to mergePositionRows',
    );
  });
});

// ── 14. Reuse: MarketPulse.svelte imports from pulseUnified ───────────────────

describe('Reuse: import guard', () => {
  const svelteSrc = readFileSync(
    resolve(FRONTEND_SRC, 'lib', 'MarketPulse.svelte'), 'utf-8'
  );

  test('mergeWatchlistRows imported from pulseUnified', () => {
    assert.ok(
      svelteSrc.includes("from '$lib/data/pulseUnified'"),
      'pulseUnified import present in MarketPulse.svelte',
    );
    assert.ok(
      svelteSrc.includes('mergeWatchlistRows'),
      'mergeWatchlistRows referenced',
    );
  });

  test('finalizeRows imported from pulseUnified', () => {
    assert.ok(svelteSrc.includes('finalizeRows'), 'finalizeRows referenced');
  });

  test('sortUnifiedRows imported from pulseUnified', () => {
    assert.ok(svelteSrc.includes('sortUnifiedRows'), 'sortUnifiedRows referenced');
  });
});

// ── 15. SSOT: MCX stale-ticker Day P&L — virtual-root regression ─────────────
//
// Reproduces the reported symptom: "adding CRUDEOIL to positions in Pulse
// causes Day P&L to become 0."
//
// The stale-ticker fingerprint (MCX CRUDEOIL): broker ships last_price ===
// close_price so day_change_val collapses to 0. With an overnight position
// (oq > 0) baseDayPnlForPosition returns 0 (not the pnl fallback which
// only fires when oq === 0). Day P&L is non-zero only when a live SSE tick
// rescues it via livePositionDayPnl.
//
// Four permutations test the discriminating dimensions:
//   A  market closed, no live tick  → day_pnl MUST be 0 (stale fingerprint)
//   B  market open,  no live tick   → day_pnl MUST be 0 (tick not yet arrived)
//   C  market open,  live tick      → day_pnl MUST be non-zero (rescued)
//   D  RELIANCE control — unaffected in all permutations
//
// Watchlist-presence test: preceding mergeWatchlistRows with a CRUDEOIL
// virtual-root entry must NOT change mergePositionRows output — confirming
// the helpers are decoupled and the bug (if it reproduces) lives elsewhere.

describe('MCX stale-ticker Day P&L (virtual-root regression)', () => {
  // Stale overnight MCX position: last_price === close_price → dcv = 0.
  const CRUDE_ROW = {
    tradingsymbol: 'CRUDEOIL26JUNFUT',
    exchange: 'MCX',
    quantity: 100,
    average_price: 6000,
    last_price: 6050,
    close_price: 6050,   // stale: same as last_price → dcv = 0 at broker
    day_change_val: 0,
    overnight_quantity: 100,
    pnl: 5000,
    realised: 0,
    account: 'ZG',
  };

  // Control equity position — unaffected by MCX stale path.
  const RELIANCE_ROW = {
    tradingsymbol: 'RELIANCE',
    exchange: 'NSE',
    quantity: 10,
    average_price: 3000,
    last_price: 3100,
    close_price: 3050,
    day_change_val: 500,
    overnight_quantity: 10,
    pnl: 1000,
    realised: 0,
    account: 'ZG',
  };

  // Helper builds a posCtx with the REAL nav.js functions.
  function realCtx(marketOpen) {
    return {
      snapOf: () => null,
      getInst: null,
      isMarketOpen: () => marketOpen,
      baseDayPnlForPosition,  // real implementation from nav.js
      directional: (cp) => cp,
      leadAccount: (row) => row.accounts && row.accounts.size > 0 ? [...row.accounts][0] : null,
      acctColor: () => null,
    };
  }

  // ── A: market CLOSED, no live tick ──────────────────────────────────────────
  test('A — closed market, stale MCX: day_pnl is 0', () => {
    const byKey = makeByKey();
    mergePositionRows(byKey, [CRUDE_ROW], true, {}, realCtx(false));
    const row = byKey[`CRUDEOIL26JUNFUT${MAJOR_SUFFIX.positions}`];
    assert.ok(row, 'row created');
    // baseDayPnlForPosition: oq=100 > 0, so returns dcv = 0. No live tick.
    assert.equal(row.day_pnl, 0, 'closed market + no tick → day_pnl 0');
    assert.equal(row._broker_day_pnl, 0, '_broker_day_pnl 0 (dcv=0, oq>0)');
  });

  // ── B: market OPEN, no live tick ────────────────────────────────────────────
  test('B — open market, stale MCX, no live tick: day_pnl is 0', () => {
    const byKey = makeByKey();
    mergePositionRows(byKey, [CRUDE_ROW], true, {}, realCtx(true));
    const row = byKey[`CRUDEOIL26JUNFUT${MAJOR_SUFFIX.positions}`];
    // legLiveLtp = null (empty cq), so livePositionDayPnl returns brokerDcv = 0.
    assert.equal(row.day_pnl, 0, 'open market + no live tick → still 0');
    assert.equal(row._broker_day_pnl, 0);
  });

  // ── C: market OPEN, live tick available → rescue path ───────────────────────
  test('C — open market, live tick: day_pnl rescued from (ltp - close) * qty', () => {
    const byKey = makeByKey();
    // Simulate a live SSE tick: ltp has moved to 6080 vs close 6050.
    const liveTick = 6080;
    const cq = { 'MCX:CRUDEOIL26JUNFUT': { ltp: liveTick } };
    mergePositionRows(byKey, [CRUDE_ROW], true, cq, realCtx(true));
    const row = byKey[`CRUDEOIL26JUNFUT${MAJOR_SUFFIX.positions}`];
    // Expected: realisedToday = dcv - (pollLtp - closePx)*qty = 0 - (6050-6050)*100 = 0
    //           result = 0 + (6080 - 6050) * 100 = 3000
    const expected = (liveTick - CRUDE_ROW.close_price) * CRUDE_ROW.quantity;
    assert.equal(row.day_pnl, expected, 'live tick rescues day_pnl');
    // _broker_day_pnl mirrors raw broker snapshot — remains 0.
    assert.equal(row._broker_day_pnl, 0, '_broker_day_pnl stays 0 (broker dcv=0)');
  });

  // ── D: RELIANCE control — unaffected in all permutations ────────────────────
  test('D — RELIANCE control: day_pnl = dcv = 500 regardless of MCX state', () => {
    const byKey = makeByKey();
    mergePositionRows(byKey, [RELIANCE_ROW], true, {}, realCtx(true));
    const row = byKey[`RELIANCE${MAJOR_SUFFIX.positions}`];
    // baseDayPnlForPosition: oq=10 > 0, dcv=500, returns dcv. No live tick.
    assert.equal(row.day_pnl, 500, 'RELIANCE day_pnl = dcv');
    assert.equal(row._broker_day_pnl, 500);
  });

  // ── E: watchlist CRUDEOIL virtual-root does NOT affect position row ──────────
  test('E — watchlist CRUDEOIL presence does NOT change position Day P&L', () => {
    // Without watchlist row.
    const byKeyNoWl = makeByKey();
    mergePositionRows(byKeyNoWl, [CRUDE_ROW, RELIANCE_ROW], true, {}, realCtx(true));

    // With watchlist row for virtual root CRUDEOIL.
    const byKeyWl = makeByKey();
    const actLists = [{
      id: 99, is_pinned: false,
      items: [{ tradingsymbol: 'CRUDEOIL', exchange: 'MCX', id: 99, alias: null }],
    }];
    mergeWatchlistRows(byKeyWl, actLists, { snapOf: () => null, getInst: null });
    mergePositionRows(byKeyWl, [CRUDE_ROW, RELIANCE_ROW], true, {}, realCtx(true));

    const crudePosKey    = `CRUDEOIL26JUNFUT${MAJOR_SUFFIX.positions}`;
    const reliancePosKey = `RELIANCE${MAJOR_SUFFIX.positions}`;

    // Position day_pnl must be identical whether or not watchlist has CRUDEOIL.
    assert.equal(
      byKeyWl[crudePosKey].day_pnl,
      byKeyNoWl[crudePosKey].day_pnl,
      'CRUDEOIL watchlist entry does not alter CRUDEOIL26JUNFUT position day_pnl',
    );
    assert.equal(
      byKeyWl[reliancePosKey].day_pnl,
      byKeyNoWl[reliancePosKey].day_pnl,
      'CRUDEOIL watchlist entry does not alter RELIANCE position day_pnl',
    );

    // Virtual-root watchlist row exists; actual-contract watchlist row does not.
    const crudeWlKey = `CRUDEOIL${MAJOR_SUFFIX.watchlist}`;
    assert.ok(byKeyWl[crudeWlKey], 'CRUDEOIL watchlist row present');
    assert.equal(byKeyWl[crudePosKey]._majorGroup, 'positions', 'positions row is positions');
  });

  // ── F: fresh MCX intraday position (oq=0) — pnl-fallback fires ──────────────
  test('F — new intraday MCX position (oq=0, dcv=0): uses pnl fallback', () => {
    const intradayRow = {
      tradingsymbol: 'CRUDEOIL26JUNFUT',
      exchange: 'MCX',
      quantity: 100,
      average_price: 6000,
      last_price: 6100,
      close_price: 0,      // opened today — no prior close
      day_change_val: 0,
      overnight_quantity: 0,
      pnl: 10000,
      realised: 0,
      account: 'ZG',
    };
    const byKey = makeByKey();
    mergePositionRows(byKey, [intradayRow], true, {}, realCtx(true));
    const row = byKey[`CRUDEOIL26JUNFUT${MAJOR_SUFFIX.positions}`];
    // baseDayPnlForPosition: oq=0 && dcv=0 && pnl=10000 → returns pnl=10000.
    assert.equal(row._broker_day_pnl, 10000, 'pnl fallback fires for oq=0 intraday');
    // day_pnl also = 10000 when no live tick (livePositionDayPnl falls through to brokerDcv).
    assert.equal(row.day_pnl, 10000, 'day_pnl = pnl fallback');
  });
});

// ── 16. UX: edge cases ────────────────────────────────────────────────────────

describe('UX: edge cases', () => {
  test('empty inputs produce no rows', () => {
    const byKey = makeByKey();
    mergeWatchlistRows(byKey, [], { snapOf: () => null, getInst: null });
    mergePositionRows(byKey, [], true, {}, noSnapCtx());
    mergeHoldingRows(byKey, [], true, {}, noSnapCtx());
    mergeUnderlyingAnchors(byKey, {}, [], [], true, true, { getInst: null });
    mergeMoverRows(byKey, [], true, true, true, true, { snapOf: () => null });
    assert.equal(Object.keys(byKey).length, 0);
  });

  test('null/undefined tradingsymbol skipped gracefully', () => {
    const byKey = makeByKey();
    const pos = [{ tradingsymbol: null, quantity: 10, average_price: 100, pnl: 0, day_change_val: 0 }];
    mergePositionRows(byKey, pos, true, {}, noSnapCtx());
    assert.equal(Object.keys(byKey).length, 0);
  });

  test('finalizeRows on empty byKey is a no-op', () => {
    const byKey = makeByKey();
    assert.doesNotThrow(() => finalizeRows(byKey, {
      directional: () => null,
      leadAccount: () => null,
      acctColor: () => null,
    }));
  });

  test('sortUnifiedRows handles empty array', () => {
    const sorted = sortUnifiedRows([], {}, []);
    assert.deepEqual(sorted, []);
  });

  test('fillSymbolMeta: first-write-wins — does not overwrite existing values', () => {
    const row = { underlying: 'NIFTY', kind: 'opt', strike: 21000, opt_type: 'CE', expiry: '25APR' };
    fillSymbolMeta(row, 'NIFTY25JUN22000PE', null);
    // All existing fields must be unchanged
    assert.equal(row.underlying, 'NIFTY');
    assert.equal(row.kind,       'opt');
    assert.equal(row.strike,     21000);
    assert.equal(row.opt_type,   'CE');
    assert.equal(row.expiry,     '25APR');
  });

  test('INDEX_TO_UNDERLYING covers all expected NSE indices', () => {
    const expected = ['NIFTY 50', 'NIFTY BANK', 'SENSEX', 'INDIA VIX', 'NIFTY SMLCAP 100'];
    for (const sym of expected) {
      assert.ok(INDEX_TO_UNDERLYING[sym], `${sym} should be in INDEX_TO_UNDERLYING`);
    }
  });
});
