/**
 * pageLoad_expired.test.js — Vitest tests for expired-contract filtering
 * in buildCandidatePositions and buildCleanLegs.
 *
 * Five quality dimensions:
 *  1. SSOT  — exercises the same module path used by +page.svelte
 *  2. Perf  — all synchronous; no I/O
 *  3. Stale — guards against regressions in the expiry-filter logic
 *  4. Reuse — same helpers used by the derivatives page
 *  5. UX    — expired contracts must not reach strategy analytics backend
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { buildCandidatePositions, buildCleanLegs, buildPositionRowFromBroker, buildHoldingRowFromBroker } from '$lib/derivatives/pageLoad.js';

// Pin todayIST to a fixed date so tests are not flaky across calendar days.
vi.mock('$lib/dateFormat.js', () => ({
  todayIST: () => '2026-07-27',
}));

// ─────────────────────────────────────────────────────────────────────────────
// Shared test fixtures
// ─────────────────────────────────────────────────────────────────────────────

/** Returns a getInstrument mock that maps sym → expiry date string. */
function makeGetInst(map) {
  return (sym) => (map[sym] ? { x: map[sym] } : null);
}

/** Minimal position row for buildCandidatePositions. */
function makePos(sym, extra = {}) {
  return {
    symbol: sym,
    account: 'ZG0790',
    qty: 50,
    source: 'live',
    pnl: 1000,
    ...extra,
  };
}

const BASE_PARAMS = {
  holdings: [],
  drafts: [],
  target: 'IDFCFIRSTB',
  selectedExpiries: [],
  selectedAccounts: [],
  simActive: false,
  proxiesForTarget: () => [],
};

// ─────────────────────────────────────────────────────────────────────────────
// buildCandidatePositions — expiry filter
// ─────────────────────────────────────────────────────────────────────────────

describe('buildCandidatePositions — expired-contract filtering', () => {
  it('excludes a position whose instrument expiry is before today (yesterday)', () => {
    const getInstrument = makeGetInst({
      'IDFCFIRSTB25JUL500CE': '2026-07-24',  // last Thursday, before 2026-07-27
    });
    const positions = [makePos('IDFCFIRSTB25JUL500CE')];
    const result = buildCandidatePositions({ ...BASE_PARAMS, positions, getInstrument });
    expect(result.filter(r => r.kind === 'opt' || r.kind === 'fut')).toHaveLength(0);
  });

  it('excludes a position whose instrument expiry is today minus one day', () => {
    // 2026-07-26 < 2026-07-27 → expired
    const getInstrument = makeGetInst({
      'IDFCFIRSTB25JUL500PE': '2026-07-26',
    });
    const positions = [makePos('IDFCFIRSTB25JUL500PE')];
    const result = buildCandidatePositions({ ...BASE_PARAMS, positions, getInstrument });
    expect(result.filter(r => r.kind === 'opt')).toHaveLength(0);
  });

  it('includes a position whose instrument expiry is today (expiry day still valid)', () => {
    // '2026-07-27' is NOT < '2026-07-27' → should pass through
    const getInstrument = makeGetInst({
      'IDFCFIRSTB25JUL500CE': '2026-07-27',
    });
    const positions = [makePos('IDFCFIRSTB25JUL500CE')];
    const result = buildCandidatePositions({ ...BASE_PARAMS, positions, getInstrument });
    expect(result.filter(r => r.kind === 'opt')).toHaveLength(1);
  });

  it('includes a position whose instrument expiry is a future date', () => {
    const getInstrument = makeGetInst({
      'IDFCFIRSTB26AUG500CE': '2026-08-27',
    });
    const positions = [makePos('IDFCFIRSTB26AUG500CE')];
    const result = buildCandidatePositions({ ...BASE_PARAMS, positions, getInstrument });
    expect(result.filter(r => r.kind === 'opt')).toHaveLength(1);
  });

  it('excludes a position when instrument is not in master (removed by Kite after expiry)', () => {
    // When getInstrument returns null the instrument has been removed from the
    // master (Kite removes it after settlement). The position must be excluded.
    const positions = [makePos('IDFCFIRSTB26AUG500CE')];
    const result = buildCandidatePositions({ ...BASE_PARAMS, positions, getInstrument: () => null });
    expect(result.filter(r => r.kind === 'opt')).toHaveLength(0);
  });

  it('excludes an F&O draft when instrument is not in master (removed by Kite)', () => {
    const drafts = [
      { symbol: 'IDFCFIRSTB26AUG500CE', qty: 10, avg_cost: 5, ltp: 3, id: 3 },
    ];
    const result = buildCandidatePositions({
      ...BASE_PARAMS, positions: [], drafts, getInstrument: () => null,
    });
    expect(result.filter(r => r.source === 'draft')).toHaveLength(0);
  });

  it('excludes an equity holding with qty=0 (closed holding should not appear)', () => {
    const holdings = [
      { symbol: 'IDFCFIRSTB', account: 'ZG0790', qty: 0, opening_qty: 0 },
    ];
    const result = buildCandidatePositions({
      ...BASE_PARAMS, positions: [], holdings, getInstrument: () => null,
    });
    expect(result.filter(r => r.kind === 'eq')).toHaveLength(0);
  });

  it('excludes a proxy-hedge holding with qty=0', () => {
    const holdings = [
      { symbol: 'GOLDBEES', account: 'ZG0790', qty: 0, opening_qty: 0 },
    ];
    const result = buildCandidatePositions({
      ...BASE_PARAMS,
      target: 'GOLD',
      positions: [],
      holdings,
      proxiesForTarget: () => ['GOLDBEES'],
      getInstrument: () => null,
    });
    expect(result.filter(r => r.kind === 'eq')).toHaveLength(0);
  });

  it('excludes expired draft position', () => {
    const getInstrument = makeGetInst({
      'IDFCFIRSTB25JUL500CE': '2026-07-24',
    });
    const drafts = [
      { symbol: 'IDFCFIRSTB25JUL500CE', qty: 10, avg_cost: 5, ltp: 3, id: 1 },
    ];
    const result = buildCandidatePositions({
      ...BASE_PARAMS, positions: [], drafts, getInstrument,
    });
    expect(result.filter(r => r.source === 'draft')).toHaveLength(0);
  });

  it('includes non-expired draft position', () => {
    const getInstrument = makeGetInst({
      'IDFCFIRSTB26AUG500CE': '2026-08-28',
    });
    const drafts = [
      { symbol: 'IDFCFIRSTB26AUG500CE', qty: 10, avg_cost: 5, ltp: 3, id: 2 },
    ];
    const result = buildCandidatePositions({
      ...BASE_PARAMS, positions: [], drafts, getInstrument,
    });
    expect(result.filter(r => r.source === 'draft')).toHaveLength(1);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// buildCleanLegs — expiry safety-net filter
// ─────────────────────────────────────────────────────────────────────────────

describe('buildCleanLegs — expired-leg safety-net filter', () => {
  it('excludes a leg whose expiry is before today', () => {
    // Expiry pre-populated on the leg object (as buildCleanLegs does via inst?.x)
    // We use a getInstrument that returns a past expiry so the map step sets it.
    const getInst = makeGetInst({ 'IDFCFIRSTB25JUL500CE': '2026-07-24' });
    const legs = [
      { kind: 'opt', symbol: 'IDFCFIRSTB25JUL500CE', qty: 50, avg_cost: 5, source: 'live' },
    ];
    const result = buildCleanLegs(legs, getInst);
    expect(result).toHaveLength(0);
  });

  it('excludes a leg whose expiry is yesterday', () => {
    const getInst = makeGetInst({ 'IDFCFIRSTB25JUL500PE': '2026-07-26' });
    const legs = [
      { kind: 'opt', symbol: 'IDFCFIRSTB25JUL500PE', qty: 25, avg_cost: 3, source: 'live' },
    ];
    const result = buildCleanLegs(legs, getInst);
    expect(result).toHaveLength(0);
  });

  it('includes a leg whose expiry is today (still valid on expiry day)', () => {
    // '2026-07-27' < '2026-07-27' is false → leg passes through
    const getInst = makeGetInst({ 'IDFCFIRSTB25JUL500CE': '2026-07-27' });
    const legs = [
      { kind: 'opt', symbol: 'IDFCFIRSTB25JUL500CE', qty: 50, avg_cost: 5, source: 'live' },
    ];
    const result = buildCleanLegs(legs, getInst);
    expect(result).toHaveLength(1);
  });

  it('includes a leg whose expiry is a future date', () => {
    const getInst = makeGetInst({ 'IDFCFIRSTB26AUG500CE': '2026-08-27' });
    const legs = [
      { kind: 'opt', symbol: 'IDFCFIRSTB26AUG500CE', qty: 50, avg_cost: 5, source: 'live' },
    ];
    const result = buildCleanLegs(legs, getInst);
    expect(result).toHaveLength(1);
  });

  it('includes a leg with no expiry in instruments cache (fail-open)', () => {
    // When getInstrument returns null, expiry=null → guard condition `l.expiry && ...` is falsy
    const legs = [
      { kind: 'opt', symbol: 'IDFCFIRSTB26AUG500CE', qty: 50, avg_cost: 5, source: 'live' },
    ];
    const result = buildCleanLegs(legs, () => null);
    expect(result).toHaveLength(1);
  });

  it('filters correctly when mixing expired and non-expired legs', () => {
    const getInst = makeGetInst({
      'IDFCFIRSTB25JUL500CE': '2026-07-24',   // expired
      'IDFCFIRSTB26AUG500CE': '2026-08-27',   // future
    });
    const legs = [
      { kind: 'opt', symbol: 'IDFCFIRSTB25JUL500CE', qty: 50, avg_cost: 5, source: 'live' },
      { kind: 'opt', symbol: 'IDFCFIRSTB26AUG500CE', qty: 25, avg_cost: 8, source: 'live' },
    ];
    const result = buildCleanLegs(legs, getInst);
    expect(result).toHaveLength(1);
    expect(result[0].symbol).toBe('IDFCFIRSTB26AUG500CE');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// buildPositionRowFromBroker — prev_close field priority
// ─────────────────────────────────────────────────────────────────────────────

describe('buildPositionRowFromBroker — prev_close uses previous_close over close_price', () => {
  it('uses previous_close when close_price is 0 (stale overnight)', () => {
    const row = buildPositionRowFromBroker({
      tradingsymbol: 'NIFTY25SEP24000CE',
      previous_close: 500,
      close_price: 0,
      quantity: 50,
      account: 'ZG0790',
    }, 'live');
    expect(row.prev_close).toBe(500);
  });

  it('falls back to close_price when previous_close is 0 or absent', () => {
    const row = buildPositionRowFromBroker({
      tradingsymbol: 'NIFTY25SEP24000CE',
      previous_close: 0,
      close_price: 300,
      quantity: 50,
      account: 'ZG0790',
    }, 'live');
    expect(row.prev_close).toBe(300);
  });

  it('previous_close wins when both fields are non-zero', () => {
    const row = buildPositionRowFromBroker({
      tradingsymbol: 'NIFTY25SEP24000CE',
      previous_close: 500,
      close_price: 300,
      quantity: 50,
      account: 'ZG0790',
    }, 'live');
    expect(row.prev_close).toBe(500);
  });

  it('returns null when both previous_close and close_price are absent', () => {
    const row = buildPositionRowFromBroker({
      tradingsymbol: 'NIFTY25SEP24000CE',
      quantity: 50,
      account: 'ZG0790',
    }, 'live');
    expect(row.prev_close).toBeNull();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// buildHoldingRowFromBroker — prev_close field priority
// ─────────────────────────────────────────────────────────────────────────────

describe('buildHoldingRowFromBroker — prev_close uses previous_close over close_price', () => {
  it('uses previous_close when close_price is 0 (stale overnight)', () => {
    const row = buildHoldingRowFromBroker({
      tradingsymbol: 'RELIANCE',
      previous_close: 500,
      close_price: 0,
      quantity: 10,
      opening_quantity: 10,
      account: 'ZG0790',
    });
    expect(row.prev_close).toBe(500);
  });

  it('falls back to close_price when previous_close is 0 or absent', () => {
    const row = buildHoldingRowFromBroker({
      tradingsymbol: 'RELIANCE',
      previous_close: 0,
      close_price: 300,
      quantity: 10,
      opening_quantity: 10,
      account: 'ZG0790',
    });
    expect(row.prev_close).toBe(300);
  });

  it('previous_close wins when both fields are non-zero', () => {
    const row = buildHoldingRowFromBroker({
      tradingsymbol: 'RELIANCE',
      previous_close: 500,
      close_price: 300,
      quantity: 10,
      opening_quantity: 10,
      account: 'ZG0790',
    });
    expect(row.prev_close).toBe(500);
  });
});
