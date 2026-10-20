/**
 * pulseUnified.test.js
 *
 * Historical: this file originally tested the `marketOpen` gate in
 * mergePositionRows — `livePositionDayPnl` was called with `marketOpen: true`
 * hardcoded instead of `isMarketOpen()`, so in closed hours SSE LTP ticks
 * still arriving for MCX/pre-market symbols kept shifting `row.day_pnl` on
 * each buildUnified call, causing visible ag-Grid re-render animations.
 *
 * §1 (positions/holdings LTP-source redesign) removed the entire live-tick
 * mechanism: `mergePositionRows` now calls `baseDayPnlForPosition(r)`
 * directly with NO live-tick delta and no marketOpen gate — Day P&L is
 * purely poll-driven, so the closed-hours-tick-shift bug class this file
 * guarded against is structurally impossible now (there's no live-tick input
 * left to shift the result). The describe block below is rewritten as a
 * regression guard for that stronger invariant: day_pnl is IDENTICAL
 * regardless of any SSE snapshot value, open or closed market.
 *
 * Five quality dimensions:
 *  1. SSOT   — baseDayPnlForPosition is the sole source for row.day_pnl in
 *              mergePositionRows; no ctx.isMarketOpen branch remains.
 *  2. Perf   — pure unit test, no DOM / network, sub-millisecond.
 *  3. Stale  — confirms no live-tick code path exists to reintroduce the
 *              closed-hours shift bug.
 *  4. Reuse  — uses exported mergePositionRows + real baseDayPnlForPosition
 *              from nav.js (no mocked logic copies).
 *  5. UX     — day_pnl stability (open AND closed hours) prevents the Day%
 *              cell refresh animation that triggered the original fix.
 */

import { describe, it, expect } from 'vitest';
import {
  mergePositionRows, mergeHoldingRows, makeRowFactory, mergeUnderlyingAnchors,
  totalsRowFor, finalizeRows,
} from '../../data/pulseUnified.js';
import { baseDayPnlForPosition, dayChangePct } from '$lib/data/nav.js';
import { directional } from '$lib/format.js';

// ── Shared test fixtures ──────────────────────────────────────────────────────

/**
 * Minimal position broker row that has a valid overnight position with a
 * known day_change_val (brokerDcv) and a close price, so we can distinguish
 * the live-LTP path from the brokerDcv fallback path.
 */
function makeOvernightPositionRow(overrides = {}) {
  return {
    tradingsymbol:       'NIFTY25AUG24000CE',
    exchange:            'NFO',
    quantity:            50,
    average_price:       120,
    last_price:          130,
    previous_close:      125,   // close > 0 → live path is (live - 125) * 50
    close_price:         125,
    pnl:                 500,
    day_change_val:      300,   // vestigial — no longer read by the formula
    overnight_quantity:  50,
    realised:            0,
    prev_settlement_pnl: 200,   // pnl(500) - day_change_val(300) → base=300 (brokerDcv-equivalent)
    ...overrides,
  };
}

/**
 * Build the ctx bag for mergePositionRows.
 *
 * @param {Record<string, any>} snapMap  symbol → snap object
 */
function makeCtx(snapMap = {}) {
  return {
    snapOf: (sym) => snapMap[sym] ?? null,
    getInst: null,
    baseDayPnlForPosition,
  };
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('mergePositionRows — day_pnl is poll-only, no live-tick input (§1)', () => {
  it('day_pnl returns the stable base regardless of any live SSE LTP in the snap', () => {
    const byKey = {};
    const row = makeOvernightPositionRow();
    // Confirm base (baseDayPnlForPosition) = 300 so we can assert it IS used.
    const expectedBase = baseDayPnlForPosition(row);
    expect(expectedBase).toBe(300);

    // A live SSE LTP diverging from last_price (140 vs 130) has no effect —
    // there is no live-tick delta code path left in mergePositionRows.
    const snap = { ltp: 140 };
    const ctx = makeCtx({ NIFTY25AUG24000CE: snap });

    mergePositionRows(byKey, [row], true, {}, ctx);

    // 2026-10 Part A: lookupKey is always `${sym}::${account}` — this
    // fixture has no `account` override, so the key carries an empty
    // account segment.
    const result = byKey['NIFTY25AUG24000CE::__pos'];
    expect(result).toBeDefined();
    expect(result.day_pnl).toBe(300);
    expect(result.day_pnl).not.toBe(800);
  });

  it('day_pnl is stable across two buildUnified calls with different MCX SSE LTPs', () => {
    // Simulates SSE ticks arriving — day_pnl must not shift either way, open
    // or closed market, since there's no live-tick input left (§1).
    const row = makeOvernightPositionRow({ tradingsymbol: 'CRUDEOIL25AUGFUT', exchange: 'MCX' });

    const run = (ltp) => {
      const byKey = {};
      const ctx = makeCtx({ CRUDEOIL25AUGFUT: { ltp } });
      mergePositionRows(byKey, [row], true, {}, ctx);
      return byKey['CRUDEOIL25AUGFUT::__pos'].day_pnl;
    };

    const first  = run(6200);
    const second = run(6350);  // different SSE LTP — simulates next tick

    expect(first).toBe(300);
    expect(second).toBe(300);
    expect(first).toBe(second);
  });

  it('no snap ltp at all: still returns baseDayPnlForPosition (no live-tick fallback branch)', () => {
    const byKey = {};
    const row = makeOvernightPositionRow();
    const ctx = makeCtx({});

    mergePositionRows(byKey, [row], true, {}, ctx);

    const result = byKey['NIFTY25AUG24000CE::__pos'];
    expect(result.day_pnl).toBe(300);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// mergeHoldingRows — holdClose guard (holdClose <= 0)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Build a minimal holdings broker row.
 * @param {Partial<any>} overrides
 * @returns {any}
 */
function makeHoldingRow(overrides = {}) {
  return {
    tradingsymbol:   'TCS',
    exchange:        'NSE',
    symbol:          'TCS',
    quantity:        100,
    average_price:   2500,
    last_price:      2700,
    prev_close:      2650,   // Default to > 0 — can be overridden
    close_price:     2650,   // Will be ignored per the fix
    pnl:             20000,
    day_change_val:  500,    // dcv — fallback when holdClose <= 0
    ...overrides,
  };
}

/**
 * Build ctx for mergeHoldingRows.
 * @param {Record<string, any>} snapMap  symbol → snap object
 * @returns {any}
 */
function makeHoldingCtx(snapMap = {}) {
  return {
    snapOf: (sym) => snapMap[sym] ?? null,
    getInst: null,
    isMarketOpen: () => true, // Standard — holdings snapshot logic doesn't gate on market open
  };
}

describe('mergeHoldingRows — holdClose guard', () => {
  it('holdClose < 0: uses dcv (guard fires for negative prev_close)', () => {
    const byKey = {};
    const holdingRow = makeHoldingRow({ prev_close: -1, day_change_val: 500 });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // holdClose = -1 → guard fires → uses day_change_val = 500
    expect(result.day_pnl).toBe(500);
  });

  it('holdClose = 0: uses dcv', () => {
    const byKey = {};
    const holdingRow = makeHoldingRow({ prev_close: 0, close_price: 0, day_change_val: 400 });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // holdClose = 0 → guard fires → uses day_change_val = 400
    expect(result.day_pnl).toBe(400);
  });

  it('holdClose > 0 with valid ltp: uses (ltp - holdClose) * qty when epsilon check passes', () => {
    const byKey = {};
    // prev_close: 2650, ltp: 2700, qty: 100
    // day_pnl = (2700 - 2650) * 100 = 5000
    const holdingRow = makeHoldingRow({
      prev_close: 2650,
      last_price: 2700,
      quantity: 100,
      day_change_val: 0, // Not used
    });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // holdClose = 2650 > 0, epsilon check: |2700 - 2650| = 50 > 0.005 → passes
    // day_pnl = (2700 - 2650) * 100 = 5000
    expect(result.day_pnl).toBe(5000);
  });

  it('close_price is ignored for holdClose: only prev_close is used', () => {
    const byKey = {};
    // prev_close: 0 (should fire guard), close_price: 2650 (should be ignored)
    const holdingRow = makeHoldingRow({
      prev_close: 0,
      close_price: 2650,
      day_change_val: 350,
    });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // Old buggy code would compute: (2700 - 2650) * 100 = 5000
    // Fixed code: holdClose = Number(prev_close) || Number(close_price) = 0
    // Guard fires → uses day_change_val = 350
    expect(result.day_pnl).toBe(350);
    expect(result.day_pnl).not.toBe(5000); // Not the close_price formula result
  });

  it('holdClose > 0 but epsilon check fails: uses dcv', () => {
    const byKey = {};
    // prev_close: 2650.002, ltp: 2650 (post-settlement, within 0.005 epsilon)
    // |2650 - 2650.002| = 0.002 ≤ 0.005 → epsilon check fails → uses dcv
    const holdingRow = makeHoldingRow({
      prev_close: 2650.002,
      last_price: 2650,
      day_change_val: 600,
    });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // epsilon check fails → fallback to dcv
    expect(result.day_pnl).toBe(600);
  });

  it('holdClose = holdAvg: uses dcv (Guard 2 — lifetime vs day P&L)', () => {
    const byKey = {};
    // average_price: 2500, prev_close: 2500 (same)
    // Guard 2 fires → uses dcv, not lifetime formula
    const holdingRow = makeHoldingRow({
      average_price: 2500,
      prev_close: 2500,
      last_price: 2700,
      quantity: 100,
      day_change_val: 450,
    });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // holdClose = 2500 = holdAvg → Guard 2 fires → uses dcv = 450
    expect(result.day_pnl).toBe(450);
  });

  it('quantity = 0 (closed holding): day_pnl still uses proper formula', () => {
    const byKey = {};
    const holdingRow = makeHoldingRow({
      quantity: 0,
      prev_close: 2650,
      last_price: 2700,
      day_change_val: 0,
    });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // qty = 0 → (2700 - 2650) * 0 = 0
    expect(result.day_pnl).toBe(0);
  });

  it('day_pnl is poll-only: snap (SSE-tick) ltp is ignored, only last_price/liveQ feed the formula (item-8 fix)', () => {
    const byKey = {};
    const holdingRow = makeHoldingRow({
      prev_close: 2600,
      last_price: 2700, // poll-sourced — THIS drives day_pnl now
      quantity: 100,
    });
    // snap.ltp: 2750 — an SSE tick diverging from last_price. Prior to the
    // item-8 fix this drove day_pnl (liveHold preferred snap); now day_pnl
    // is poll-only (matches portfolioStore's _holdTier1 / NavStrip H —
    // §1's poll-only redesign) so the SSE tick has NO effect on day_pnl.
    const ctx = makeHoldingCtx({ TCS: { ltp: 2750 } });

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result).toBeDefined();
    // pollHold = 2700 (last_price, NOT the 2750 snap tick), holdClose = 2600
    // day_pnl = (2700 - 2600) * 100 = 10000
    expect(result.day_pnl).toBe(10000);
    expect(result.day_pnl).not.toBe(15000); // NOT the snap-driven (2750-2600)*100 result
  });

  it('total P&L (row.pnl) still uses the live SSE tick — only day_pnl is poll-only', () => {
    const byKey = {};
    const holdingRow = makeHoldingRow({
      average_price: 2500,
      prev_close: 2600,
      last_price: 2700,
      quantity: 100,
    });
    const ctx = makeHoldingCtx({ TCS: { ltp: 2750 } });

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    // row.pnl (lifetime P&L / LTP display) is unaffected by item-8 — it
    // still prefers the live snap tick: (2750 - 2500) * 100 = 25000.
    expect(result.pnl).toBe(25000);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// mergeHoldingRows — row.close display fallback (close_price → prev_close
// rename fix, 2026-10). Distinct from the holdClose/day_pnl guard above —
// this covers the DISPLAY field (Pulse's close/Chg% column), which reads
// `r.close_price` (removed from HoldingRow — see backend commit 933a9a88)
// via a separate code path (_snapLtp-gated block + the !_hadLtp fallback).
// Before the fix: `Number(undefined) ?? null` evaluates to NaN (the `??`
// operator never triggers on NaN, only null/undefined), so row.close
// silently became NaN instead of falling back to prev_close.
// ─────────────────────────────────────────────────────────────────────────────

describe('mergeHoldingRows — row.close reads prev_close, not the removed close_price', () => {
  it('snap has ltp but no close: row.close falls back to prev_close (not NaN)', () => {
    const byKey = {};
    const holdingRow = makeHoldingRow({ prev_close: 2650, close_price: 9999 });
    // snap.ltp present, snap.close absent → _applyQuoteFields leaves
    // row.close null, so the _snapLtp-gated fallback (line ~533) fires.
    const ctx = makeHoldingCtx({ TCS: { ltp: 2700 } });

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result.close).toBe(2650);
    expect(result.close).not.toBeNaN();
    expect(result.close).not.toBe(9999); // close_price must be ignored
  });

  it('no snap, no liveQ: row.close falls back to prev_close via the !_hadLtp branch', () => {
    const byKey = {};
    const holdingRow = makeHoldingRow({ prev_close: 2650, close_price: 9999, last_price: 2700 });
    const ctx = makeHoldingCtx(); // no snap for TCS

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result.close).toBe(2650);
    expect(result.close).not.toBeNaN();
  });

  it('prev_close unavailable (0.0 default): row.close stays null, never NaN', () => {
    const byKey = {};
    const holdingRow = makeHoldingRow({ prev_close: 0, close_price: 9999, last_price: 2700 });
    const ctx = makeHoldingCtx();

    mergeHoldingRows(byKey, [holdingRow], true, {}, ctx);

    const result = byKey['TCS__hold'];
    expect(result.close).not.toBeNaN();
    expect(result.close == null).toBe(true);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// mergePositionRows — row.close fallback to prev_close (item 2, 2026-10).
// Mirrors mergeHoldingRows' close fallback above — mergePositionRows had NO
// equivalent fallback at all before this fix: a position with no symbolStore
// snap.close AND no contracts-quote-bag liveQ.close stayed row.close == null
// forever, even though the broker row's own prev_close (daily_book.ltp
// basis, per CLAUDE.md) was right there on `r.prev_close`.
// ─────────────────────────────────────────────────────────────────────────────

describe('mergePositionRows — row.close falls back to prev_close (item 2)', () => {
  it('snap has ltp but no close: row.close falls back to r.prev_close', () => {
    const byKey = {};
    const row = makeOvernightPositionRow({ prev_close: 125, close_price: 9999 });
    // snap.ltp present, snap.close absent → _applyQuoteFields leaves
    // row.close null, so the new fallback fires.
    const ctx = makeCtx({ NIFTY25AUG24000CE: { ltp: 130 } });

    mergePositionRows(byKey, [row], true, {}, ctx);

    const result = byKey['NIFTY25AUG24000CE::__pos'];
    expect(result.close).toBe(125);
    expect(result.close).not.toBe(9999); // the removed close_price field must be ignored
  });

  it('no snap, no liveQ: row.close falls back to r.prev_close via the !_hadLtp path', () => {
    const byKey = {};
    const row = makeOvernightPositionRow({ prev_close: 125, last_price: 130 });
    const ctx = makeCtx({}); // no snap for this symbol at all

    mergePositionRows(byKey, [row], true, {}, ctx);

    const result = byKey['NIFTY25AUG24000CE::__pos'];
    expect(result.close).toBe(125);
    expect(result.ltp).toBe(130); // last_price fallback still applies too
  });

  it('prev_close unavailable (0.0 default): row.close stays null, never NaN/0', () => {
    const byKey = {};
    const row = makeOvernightPositionRow({ prev_close: 0, last_price: 130 });
    const ctx = makeCtx({});

    mergePositionRows(byKey, [row], true, {}, ctx);

    const result = byKey['NIFTY25AUG24000CE::__pos'];
    expect(result.close).not.toBeNaN();
    expect(result.close == null).toBe(true);
  });

  it('close already resolved via symbolStore snap: fallback does not override it', () => {
    const byKey = {};
    const row = makeOvernightPositionRow({ prev_close: 999 }); // would be wrong if used
    const ctx = makeCtx({ NIFTY25AUG24000CE: { ltp: 130, close: 125 } });

    mergePositionRows(byKey, [row], true, {}, ctx);

    const result = byKey['NIFTY25AUG24000CE::__pos'];
    expect(result.close).toBe(125);
  });
});

// ============================================================================
// mergePositionRows — position-state field propagation (2026-10 fix,
// REWRITTEN 2026-10 Part A)
//
// is_orphan/pair_group_key/has_gtt were never copied from the
// raw broker row onto the aggregated pulse row, so MarketPulse's "St"
// column (pulseColumns.js) and the row-pos-orphan/row-pos-paired CSS
// classes (MarketPulse.svelte's _sourceRowClasses) were structurally dead
// — every position showed as unpaired regardless of real backend state.
//
// Part A (2026-10) made every position row single-account BY CONSTRUCTION
// (lookupKey always bakes in the account) — a symbol held in 2+ accounts
// now produces 2+ SEPARATE rows instead of one ambiguous merged row, so
// the old "resets to null once a second account merges in" test below is
// INVERTED: these fields are now always trustworthy, straight off the raw
// row, regardless of how many OTHER accounts also hold the same symbol.
// ============================================================================

describe('mergePositionRows — position-state field propagation (2026-10 fix)', () => {
  it('single-account row carries is_orphan/pair_group_key/has_gtt from the raw row', () => {
    const byKey = {};
    const row = makeOvernightPositionRow({
      account: 'ACC1', is_orphan: false, pair_group_key: 'P1', has_gtt: true,
    });
    mergePositionRows(byKey, [row], true, {}, makeCtx());
    const result = byKey['NIFTY25AUG24000CE::ACC1__pos'];
    expect(result.is_orphan).toBe(false);
    expect(result.pair_group_key).toBe('P1');
    expect(result.has_gtt).toBe(true);
  });

  it('single-account orphan row carries is_orphan=true with no pair key', () => {
    const byKey = {};
    const row = makeOvernightPositionRow({ account: 'ACC1', is_orphan: true });
    mergePositionRows(byKey, [row], true, {}, makeCtx());
    const result = byKey['NIFTY25AUG24000CE::ACC1__pos'];
    expect(result.is_orphan).toBe(true);
    expect(result.pair_group_key).toBeNull();
    expect(result.has_gtt).toBe(false);
  });

  it('two accounts holding the same symbol produce TWO separate rows, each carrying its own correct state (2026-10 Part A — was a single merged/nulled row)', () => {
    const byKey = {};
    const rowA = makeOvernightPositionRow({ account: 'ACC1', pair_group_key: 'P1', has_gtt: true });
    const rowB = makeOvernightPositionRow({ account: 'ACC2', pair_group_key: 'P7', is_orphan: true });
    mergePositionRows(byKey, [rowA, rowB], true, {}, makeCtx());

    const resultA = byKey['NIFTY25AUG24000CE::ACC1__pos'];
    const resultB = byKey['NIFTY25AUG24000CE::ACC2__pos'];
    expect(resultA).toBeDefined();
    expect(resultB).toBeDefined();
    expect(resultA).not.toBe(resultB);

    // Each row is single-account (never merged) — accounts.size is always 1.
    expect(resultA.accounts.size).toBe(1);
    expect(resultB.accounts.size).toBe(1);

    // ACC1's row keeps its own pair_group_key/has_gtt, not nulled.
    expect(resultA.pair_group_key).toBe('P1');
    expect(resultA.has_gtt).toBe(true);
    expect(resultA.is_orphan).toBe(false);

    // ACC2's row keeps its own pair_group_key/is_orphan, not nulled.
    expect(resultB.pair_group_key).toBe('P7');
    expect(resultB.is_orphan).toBe(true);
    expect(resultB.has_gtt).toBe(false);

    // The bare (no-account) lookup key is never populated — Part A always
    // bakes the account into the key, even when there's only one row.
    expect(byKey['NIFTY25AUG24000CE__pos']).toBeUndefined();
  });

  it('repeated rows for the SAME account (e.g. a closed/open split) merge into one row and keep the single-account fields', () => {
    const byKey = {};
    const rowA = makeOvernightPositionRow({ account: 'ACC1', pair_group_key: 'P2', has_gtt: false });
    const rowB = makeOvernightPositionRow({ account: 'ACC1', pair_group_key: 'P2', has_gtt: false });
    mergePositionRows(byKey, [rowA, rowB], true, {}, makeCtx());
    const result = byKey['NIFTY25AUG24000CE::ACC1__pos'];
    expect(result.accounts.size).toBe(1);
    expect(result.pair_group_key).toBe('P2');
  });
});

// ============================================================================
// mergePositionRows — lot-pairing split pieces get a composite bucket key
// (2026-10 lotPairSplit.js wiring)
//
// Two pieces of the SAME account+symbol (produced by splitPairedLots when a
// row spans 2+ pair groups) would otherwise collide into ONE byKey bucket
// (keyed by bare tradingsymbol), silently recreating the exact backend bug
// this split exists to fix. Composite key (symbol+account+piece label)
// keeps each piece its own row.
// ============================================================================

describe('mergePositionRows — lot-split pieces (_isLotSplit) get distinct buckets', () => {
  it('two _isLotSplit pieces of the same account+symbol land in separate byKey entries', () => {
    const byKey = {};
    const pieceP1 = makeOvernightPositionRow({
      account: 'ACC1', quantity: 20, is_orphan: false,
      pair_group_key: 'P1', _isLotSplit: true, lot_piece_label: 'P1',
    });
    const pieceP2 = makeOvernightPositionRow({
      account: 'ACC1', quantity: 30, is_orphan: false,
      pair_group_key: 'P2', _isLotSplit: true, lot_piece_label: 'P2',
    });
    mergePositionRows(byKey, [pieceP1, pieceP2], true, {}, makeCtx());

    const keyP1 = 'NIFTY25AUG24000CE::ACC1::P1__pos';
    const keyP2 = 'NIFTY25AUG24000CE::ACC1::P2__pos';
    expect(byKey[keyP1]).toBeDefined();
    expect(byKey[keyP2]).toBeDefined();
    expect(byKey[keyP1]).not.toBe(byKey[keyP2]);
    // The real tradingsymbol is preserved on each piece (never overwritten
    // by the composite lookup key) — order placement / symbol lookups
    // downstream must keep working.
    expect(byKey[keyP1].tradingsymbol).toBe('NIFTY25AUG24000CE');
    expect(byKey[keyP2].tradingsymbol).toBe('NIFTY25AUG24000CE');
    expect(byKey[keyP1].pair_group_key).toBe('P1');
    expect(byKey[keyP2].pair_group_key).toBe('P2');
    expect(byKey[keyP1]._isLotSplit).toBe(true);
    expect(byKey[keyP1].lot_piece_label).toBe('P1');
    expect(byKey[keyP2].lot_piece_label).toBe('P2');
    // Bare byKey['SYMBOL::ACC1__pos'] (the non-split base key, no piece
    // label) must NOT have been populated — confirms the pieces never
    // fell back to the unsuffixed key.
    expect(byKey['NIFTY25AUG24000CE::ACC1__pos']).toBeUndefined();
  });

  it('a non-split row for the same symbol uses the base symbol::account key (no lot-piece suffix)', () => {
    const byKey = {};
    const row = makeOvernightPositionRow({ account: 'ACC1', pair_group_key: 'P1' });
    mergePositionRows(byKey, [row], true, {}, makeCtx());
    expect(byKey['NIFTY25AUG24000CE::ACC1__pos']).toBeDefined();
    expect(byKey['NIFTY25AUG24000CE::ACC1__pos']._isLotSplit).toBeUndefined();
  });
});

// ============================================================================
// mergePositionRows — one row per account, no cross-account merge
// (2026-10 Part A)
// ============================================================================

describe('mergePositionRows — one row per account, no cross-account merge (2026-10 Part A)', () => {
  it('two accounts holding the same symbol produce two rows, each with its own qty/avg/day_pnl', () => {
    const byKey = {};
    const rowA = makeOvernightPositionRow({ account: 'ACC1', quantity: 50, average_price: 120 });
    const rowB = makeOvernightPositionRow({ account: 'ACC2', quantity: 30, average_price: 110 });
    mergePositionRows(byKey, [rowA, rowB], true, {}, makeCtx());

    const resultA = byKey['NIFTY25AUG24000CE::ACC1__pos'];
    const resultB = byKey['NIFTY25AUG24000CE::ACC2__pos'];
    expect(resultA).toBeDefined();
    expect(resultB).toBeDefined();
    expect(resultA).not.toBe(resultB);
    expect(resultA.qty_pos).toBe(50);
    expect(resultB.qty_pos).toBe(30);
    expect(resultA.accounts.has('ACC1')).toBe(true);
    expect(resultA.accounts.has('ACC2')).toBe(false);
    expect(resultB.accounts.has('ACC2')).toBe(true);
    expect(resultB.accounts.has('ACC1')).toBe(false);
    // Each row's own day_pnl — no cross-contamination between the two
    // account rows.
    expect(resultA.day_pnl).toBe(baseDayPnlForPosition(rowA));
    expect(resultB.day_pnl).toBe(baseDayPnlForPosition(rowB));
  });
});

// ============================================================================
// mergeUnderlyingAnchors — does not duplicate an existing per-account
// position row (2026-10 Part A follow-up)
//
// Positions rows are now keyed `${symbol}::${account}`, not bare symbol.
// An underlying anchor whose own tradingsymbol happens to match a REAL
// held position (e.g. an MCX option's underlying anchor resolves to the
// same future contract the operator also holds directly) must enrich
// that existing row in place, not create a second bare-keyed row.
// ============================================================================

describe('mergeUnderlyingAnchors — does not duplicate an existing per-account position row (2026-10 Part A)', () => {
  it('enriches the existing per-account future row in place instead of creating a second bare-keyed row', () => {
    const byKey = {};
    const futureRow = makeOvernightPositionRow({
      tradingsymbol: 'CRUDEOIL26JUNFUT', account: 'ACC1', exchange: 'MCX',
    });
    const optionRow = makeOvernightPositionRow({
      tradingsymbol: 'CRUDEOIL26JUN10500CE', account: 'ACC1', exchange: 'MCX',
    });
    mergePositionRows(byKey, [futureRow, optionRow], true, {}, makeCtx());

    const uq = {
      CRUDEOIL: {
        ltp: 6500,
        _resolved: {
          tradingsymbol: 'CRUDEOIL26JUNFUT',
          exchange: 'MCX',
          displayUnderlying: 'CRUDEOIL',
          kind: 'fut',
          _major: 'positions',
        },
      },
    };

    mergeUnderlyingAnchors(byKey, uq, [futureRow, optionRow], [], true, true, { getInst: null });

    // Exactly one row carries the future's own tradingsymbol — no second,
    // spot-only anchor row was created alongside the real position row.
    const matches = Object.values(byKey).filter(
      (r) => r._majorGroup === 'positions' && r.tradingsymbol === 'CRUDEOIL26JUNFUT');
    expect(matches.length).toBe(1);
    expect(byKey['CRUDEOIL26JUNFUT::ACC1__pos']).toBeDefined();
    expect(byKey['CRUDEOIL26JUNFUT__pos']).toBeUndefined();
    // The real position row was enriched with the anchor's own fields.
    expect(matches[0].src.u).toBe(true);
    expect(matches[0].ltp).toBe(6500);
  });
});

// ============================================================================
// totalsRowFor — Exp P&L dedup across split account-rows
// (2026-10 Part A, Change 2 — required fix alongside the per-account split)
// ============================================================================

describe('totalsRowFor — Exp P&L dedup across split account-rows (2026-10 Part A, Change 2)', () => {
  it('a symbol split into 2 account-rows contributes its cross-account Exp P&L exactly once to TOTAL', () => {
    const rowA = { tradingsymbol: 'NIFTY25AUG24000CE', qty_pos: 50, day_pnl: 100, pnl: 200, _cost_basis: 0, _prev_market_value: 0 };
    const rowB = { tradingsymbol: 'NIFTY25AUG24000CE', qty_pos: 30, day_pnl: 60,  pnl: 90,  _cost_basis: 0, _prev_market_value: 0 };
    // Already a cross-account total (same shape as MarketPulse.svelte's
    // _filteredExpPnlByKey) — must be added ONCE regardless of how many
    // account-rows represent this symbol.
    const expPnlByKey = { NIFTY25AUG24000CE: { exp_pnl: 500, extrinsic: 20 } };

    const splitTotal = totalsRowFor([rowA, rowB], 'positions', 'TOTAL Positions', expPnlByKey);

    // Un-split single-row-equivalent (one account, combined qty) must
    // produce the SAME TOTAL Exp P&L.
    const singleRow = { tradingsymbol: 'NIFTY25AUG24000CE', qty_pos: 80, day_pnl: 160, pnl: 290, _cost_basis: 0, _prev_market_value: 0 };
    const unsplitTotal = totalsRowFor([singleRow], 'positions', 'TOTAL Positions', expPnlByKey);

    expect(splitTotal.exp_pnl).toBe(500);
    expect(splitTotal.exp_pnl).toBe(unsplitTotal.exp_pnl);
    // A naive once-per-row sum would have doubled it to 1000.
    expect(splitTotal.exp_pnl).not.toBe(1000);
  });
});

// ============================================================================
// D4/D5 (2026-10-20) — Chg% denominator reads the backend's own prev_close
// exclusively (a dedicated per-leg accumulator, never row.close — which
// stays live-quote-sourced for bid/ask/depth display), and never renders
// blank for a held position with qty != 0.
// ============================================================================

function _finCtx() {
  return { directional, leadAccount: () => null, acctColor: () => null };
}

describe('D4 — _prev_market_value uses the backend prev_close SSOT, never row.close', () => {
  it('ignores a stale live-quote close and uses r.prev_close instead', () => {
    const byKey = {};
    const row = makeOvernightPositionRow({
      tradingsymbol: 'NIFTY25AUG24000CE', account: 'ACC1',
      quantity: 50, average_price: 120, prev_close: 125,
    });
    // Stale live-quote close (Kite's raw ohlc.close) diverges from the
    // backend's correct prev_close — both snap and liveQ carry it.
    const snap = { ltp: 140, close: 999 };
    mergePositionRows(byKey, [row], true, {}, {
      snapOf: () => snap, getInst: null, baseDayPnlForPosition,
    });
    finalizeRows(byKey, _finCtx());
    const out = byKey['NIFTY25AUG24000CE::ACC1__pos'];
    // row.close itself is UNCHANGED — still live-quote-sourced (additive
    // fix, not a reorder of the existing bid/ask/depth-serving chain).
    expect(out.close).toBe(999);
    // _prev_market_value must come from r.prev_close (125 × 50), never
    // from the stale live-quote close (999 × 50).
    expect(out._prev_market_value).toBe(125 * 50);
    expect(out._prev_market_value).not.toBe(999 * 50);
  });
});

describe('D4 — multi-leg accumulation sums per-leg prev_close, not a single overwritten scalar', () => {
  it('two position legs for the same account+symbol sum independently', () => {
    const byKey = {};
    const legA = makeOvernightPositionRow({
      tradingsymbol: 'CRUDEOIL26OCTFUT', account: 'ACC1',
      quantity: 10, average_price: 5700, prev_close: 5750,
    });
    const legB = makeOvernightPositionRow({
      tradingsymbol: 'CRUDEOIL26OCTFUT', account: 'ACC1',
      quantity: 20, average_price: 5720, prev_close: 5760,
    });
    mergePositionRows(byKey, [legA, legB], true, {}, makeCtx());
    finalizeRows(byKey, _finCtx());
    const out = byKey['CRUDEOIL26OCTFUT::ACC1__pos'];
    // Each leg contributes prev_close × |qty| independently — never a
    // single scalar close multiplied by the combined qty.
    expect(out._prev_market_value).toBe(5750 * 10 + 5760 * 20);
  });

  it('holdings legs from two different accounts (cross-account holdings row) sum independently', () => {
    const byKey = {};
    const legA = { tradingsymbol: 'RELIANCE', exchange: 'NSE', account: 'ACC1',
      quantity: 10, average_price: 2400, prev_close: 2450, last_price: 2480,
      pnl: 0, day_change_val: 0 };
    const legB = { tradingsymbol: 'RELIANCE', exchange: 'NSE', account: 'ACC2',
      quantity: 5, average_price: 2410, prev_close: 2460, last_price: 2480,
      pnl: 0, day_change_val: 0 };
    mergeHoldingRows(byKey, [legA, legB], true, {}, {
      snapOf: () => null, getInst: null, isMarketOpen: () => true,
    });
    finalizeRows(byKey, _finCtx());
    const out = byKey['RELIANCE__hold'];
    expect(out._prev_market_value).toBe(2450 * 10 + 2460 * 5);
  });

  it('a composite row carrying BOTH a positions leg and a holdings leg sums both accumulators, not a single overwritten scalar', () => {
    // Constructed directly — mirrors how finalizeRows already generically
    // combines _avg_num + _avg_hold_num into avg_combined/_cost_basis
    // regardless of row shape; the new prev_close accumulators must be
    // consumed the same way.
    const byKey = {
      TESTROW: {
        key: 'TESTROW', qty_pos: 10, qty_hold: 5,
        _avg_num: 1000, _avg_hold_num: 500,
        _prevCloseSsot: 1050 * 10,
        _prevCloseHoldSsot: 1060 * 5,
        close: 9999, // deliberately stale/irrelevant — must NOT be used
        accounts: new Set(),
      },
    };
    finalizeRows(byKey, _finCtx());
    const out = byKey.TESTROW;
    expect(out._prev_market_value).toBe(1050 * 10 + 1060 * 5);
  });
});

describe('D5 — never renders blank for a held position (zero-denominator fallback)', () => {
  it('falls back to |avg_cost × qty| when no leg has a prior settlement (position opened today)', () => {
    const byKey = {};
    const row = makeOvernightPositionRow({
      tradingsymbol: 'BANKNIFTY25AUG50000CE', account: 'ACC1',
      quantity: 25, average_price: 300, prev_close: 0, // opened today — no prior session close
    });
    mergePositionRows(byKey, [row], true, {}, makeCtx());
    finalizeRows(byKey, _finCtx());
    const out = byKey['BANKNIFTY25AUG50000CE::ACC1__pos'];
    // Accumulator is 0 (no prior settlement) — fallback to cost basis.
    expect(out._prev_market_value).toBe(300 * 25);
    // The Chg% cell (dayChangePct(day_pnl, _prev_market_value)) must render
    // a real percentage, never null/blank, as long as day_pnl is finite.
    const pct = dayChangePct(out.day_pnl, Math.abs(out._prev_market_value));
    expect(pct).not.toBeNull();
    expect(Number.isFinite(pct)).toBe(true);
  });
});
