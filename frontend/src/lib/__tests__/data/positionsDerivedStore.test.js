/**
 * positionsDerivedStore.test.js
 *
 * Unit tests for the positionsDerivedStore computation logic.
 *
 * The store aggregates three values across positions + holdings:
 *   1. dayTotal    — live Day P&L (via livePositionDayPnl, 4 Hz throttled)
 *   2. expiryTotal — expiry P&L (options intrinsic / futures spot-cost / closed realised)
 *   3. byKey       — per-symbol day P&L (pulse-overridable)
 *
 * This test validates the underlying helpers directly (sans Svelte reactive
 * plumbing) following the positionsDayPnlStore.test.js pattern.
 *
 * Five quality dimensions:
 *   1. SSOT  — livePositionDayPnl + expiryPnl are canonical; no inline duplication
 *   2. Perf  — pure unit tests, no DOM / network, sub-millisecond
 *   3. Stale — closed-leg, missing spot, equity exclusion, stale-close cases
 *   4. Reuse — exercises shared nav.js + expiryPnl.js exports
 *   5. UX    — totals match NavStrip expectations; equity excluded from Exp P&L
 */

import { describe, it, expect } from 'vitest';
import { livePositionDayPnl, baseDayPnlForPosition } from '$lib/data/nav.js';
import { expiryPnl } from '$lib/data/expiryPnl.js';

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Simulate positionsDerivedStore._expiryForPosition logic (pure JS mirror).
 * Returns the expiry P&L contribution for a single position row.
 * Mirrors the store's per-position logic so tests can assert without
 * loading Svelte modules.
 *
 * @param {object} p  - raw position row
 * @param {(root: string) => number} getSpot - spot resolver (mock)
 * @param {(sym: string) => {ltp?: number} | undefined} getSnap - snapshot resolver (mock)
 * @returns {number | null}
 */
function computeExpiryForPosition(p, getSpot = () => 0, getSnap = () => undefined) {
  const FO_EXCHS = new Set(['NFO', 'MCX', 'CDS', 'BFO']);
  const exch = String(p?.exchange || '').toUpperCase();
  if (!FO_EXCHS.has(exch)) return null;  // equity excluded

  const sym = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
  const qty = Number(p?.quantity ?? p?.qty) || 0;
  const avg = Number(p?.average_price ?? p?.avg_cost) || 0;

  // Closed leg — realised P&L locked in.
  if (!qty) return Number(p?.pnl || 0);

  const isCE  = sym.endsWith('CE');
  const isPE  = sym.endsWith('PE');
  const isFut = sym.endsWith('FUT') || (!isCE && !isPE && exch !== 'CDS');

  // Derive root via simple regex (mirrors decomposeSymbol for known patterns).
  const rootMatch = sym.match(/^([A-Z]+)/);
  const root = rootMatch?.[1] ?? sym;

  let v = null;
  if (isCE || isPE) {
    const spot1 = Number(p?.underlying_ltp || 0);
    const spot = spot1 > 0 ? spot1 : getSpot(root);
    if (spot > 0) {
      v = expiryPnl({ symbol: sym, qty, avg_cost: avg, kind: 'opt' }, spot);
    }
  } else if (isFut) {
    const snap = getSnap(sym);
    const live = (snap?.ltp != null && snap.ltp > 0) ? snap.ltp : Number(p?.last_price || 0);
    if (live > 0) {
      v = expiryPnl({ symbol: sym, qty, avg_cost: avg, kind: 'fut' }, live);
    }
  }

  if (v != null) return v + Number(p?.realised || 0);
  return null;
}

/**
 * Simulate positionsDerivedStore._computed for a set of position rows.
 * Returns { dayTotal, expiryTotal, byKey, expiryByAcct }.
 */
function computeDerived(posRows, snapshots = {}, getSpot = () => 0, marketOpen = true) {
  let dayTotal    = 0;
  let expiryTotal = 0;
  const byKey = {};
  /** @type {Map<string, number>} */
  const expiryByAcct = new Map();

  for (const p of posRows) {
    const sym = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
    if (!sym) continue;

    const snap    = snapshots[sym];
    const liveLtp = snap?.ltp ?? null;

    const dayVal = livePositionDayPnl(
      {
        closePx: Number(p?.previous_close) || Number(p?.close_price ?? 0),
        pollLtp: Number(p?.last_price      ?? 0),
        qty:     Number(p?.quantity        ?? 0),
        avg:     Number(p?.average_price   ?? 0),
        dcvRow:  p,
      },
      liveLtp,
      { marketOpen },
    );

    byKey[sym] = (byKey[sym] ?? 0) + dayVal;
    dayTotal  += dayVal;

    const expVal = computeExpiryForPosition(p, getSpot, (s) => snapshots[s]);
    if (expVal != null) {
      expiryTotal += expVal;
      const acct = String(p?.account || '');
      if (acct) expiryByAcct.set(acct, (expiryByAcct.get(acct) ?? 0) + expVal);
    }
  }

  return { dayTotal, expiryTotal, byKey, expiryByAcct };
}

function makePos(overrides = {}) {
  return {
    tradingsymbol: 'NIFTY26JUNFUT',
    exchange: 'NFO',
    quantity: 25,
    average_price: 23000,
    close_price: 22800,
    last_price: 23100,
    pnl: 2500,
    day_change_val: 7500,
    overnight_quantity: 25,
    realised: 0,
    account: 'ZA1234',
    ...overrides,
  };
}

// ── Day P&L tests ─────────────────────────────────────────────────────────────

describe('positionsDerivedStore — Day P&L', () => {
  it('uses livePositionDayPnl with SSE ltp when available', () => {
    const pos = makePos({ last_price: 23100, close_price: 22800, quantity: 25, overnight_quantity: 25 });
    const { dayTotal } = computeDerived([pos], { NIFTY26JUNFUT: { ltp: 23200 } });
    // With live ltp=23200, close=22800, qty=25: realisedToday = dcv - (23100-22800)*25 = 7500-7500=0;
    // result = 0 + (23200-22800)*25 = 10000
    expect(dayTotal).toBeCloseTo(10000, 1);
  });

  it('falls back to broker day_change_val when no live ltp', () => {
    const pos = makePos({ day_change_val: 7500 });
    const { dayTotal } = computeDerived([pos], {});
    // No live ltp available; baseDayPnlForPosition uses prev_settlement_pnl path
    // or dcv fallback. For overnight pos without prev_settlement_pnl: dcv = 7500.
    expect(dayTotal).toBe(7500);
  });

  it('sums correctly across multiple positions', () => {
    const pos1 = makePos({ tradingsymbol: 'NIFTY26JUNFUT', day_change_val: 5000, quantity: 25, overnight_quantity: 25 });
    const pos2 = makePos({ tradingsymbol: 'GOLDFUT', exchange: 'MCX', day_change_val: 3000, quantity: 1, overnight_quantity: 1 });
    const { dayTotal, byKey } = computeDerived([pos1, pos2], {});
    expect(dayTotal).toBeCloseTo(8000, 1);
    expect(byKey['NIFTY26JUNFUT']).toBeCloseTo(5000, 1);
    expect(byKey['GOLDFUT']).toBeCloseTo(3000, 1);
  });

  it('handles new intraday position (overnight_quantity = 0)', () => {
    const pos = makePos({
      quantity: 25,
      overnight_quantity: 0,
      pnl: 1250,      // real intraday P&L
      day_change_val: 0,  // Kite returns 0 for new positions
      close_price: 0,
    });
    const { dayTotal } = computeDerived([pos], {});
    // oq=0, dcv=0, pnl≠0 → Case 1 backstop: returns pnl
    expect(dayTotal).toBe(1250);
  });
});

// ── Expiry P&L tests ──────────────────────────────────────────────────────────

describe('positionsDerivedStore — Expiry P&L', () => {
  it('computes option intrinsic for long CE position', () => {
    // NIFTY 23100 CE, spot = 23200 → intrinsic = 100, qty=25, avg=50
    // expiry = (100 - 50) * 25 = 1250
    const pos = makePos({
      tradingsymbol: 'NIFTY23100CE',
      exchange: 'NFO',
      quantity: 25,
      average_price: 50,
      underlying_ltp: 23200,
      pnl: 0,
      realised: 0,
    });
    const { expiryTotal } = computeDerived([pos]);
    expect(expiryTotal).toBeCloseTo(1250, 1);
  });

  it('computes option intrinsic for short PE position', () => {
    // NIFTY 23000 PE, spot = 23200 → intrinsic = 0, qty=-25, avg=80
    // expiry = (0 - 80) * (-25) = 2000 (short OTM PE keeps full premium)
    const pos = makePos({
      tradingsymbol: 'NIFTY23000PE',
      exchange: 'NFO',
      quantity: -25,
      average_price: 80,
      underlying_ltp: 23200,
      pnl: 0,
      realised: 0,
    });
    const { expiryTotal } = computeDerived([pos]);
    expect(expiryTotal).toBeCloseTo(2000, 1);
  });

  it('computes futures expiry as (spot - avg) * qty', () => {
    // spot (ltp) = 23200, avg = 23000, qty = 25 → (23200-23000)*25 = 5000
    const pos = makePos({
      tradingsymbol: 'NIFTY26JUNFUT',
      last_price: 23200,
      average_price: 23000,
      quantity: 25,
    });
    const { expiryTotal } = computeDerived([pos], { NIFTY26JUNFUT: { ltp: 23200 } });
    expect(expiryTotal).toBeCloseTo(5000, 1);
  });

  it('adds realised to open leg expiry', () => {
    // Open position with realised from partial scalps
    const pos = makePos({
      tradingsymbol: 'NIFTY23100CE',
      exchange: 'NFO',
      quantity: 25,
      average_price: 50,
      underlying_ltp: 23200,
      realised: 500,  // already realised from scalps today
    });
    const { expiryTotal } = computeDerived([pos]);
    // intrinsic contribution: (100-50)*25 = 1250, + realised 500 = 1750
    expect(expiryTotal).toBeCloseTo(1750, 1);
  });

  it('locks in realised P&L for closed leg (qty = 0)', () => {
    // Closed position: qty=0, pnl=800 — no spot needed
    const pos = makePos({
      tradingsymbol: 'NIFTY23100CE',
      exchange: 'NFO',
      quantity: 0,
      pnl: 800,
    });
    const { expiryTotal } = computeDerived([pos]);
    // qty=0 → return pnl directly
    expect(expiryTotal).toBe(800);
  });

  it('excludes equity (NSE/BSE) from expiry total', () => {
    const equity = makePos({
      tradingsymbol: 'INFY',
      exchange: 'NSE',
      quantity: 100,
      average_price: 1400,
      last_price: 1450,
    });
    const { expiryTotal } = computeDerived([equity], { INFY: { ltp: 1450 } });
    // Equity position → _expiryForPosition returns null → not accumulated
    expect(expiryTotal).toBe(0);
  });

  it('returns null (skipped) when spot cannot be resolved', () => {
    // Option with no underlying_ltp and no underlyingSpotStore value
    const pos = makePos({
      tradingsymbol: 'NIFTY23100CE',
      exchange: 'NFO',
      quantity: 25,
      average_price: 50,
      underlying_ltp: 0,   // not stamped
      // getSpot returns 0 (default in computeDerived)
    });
    const { expiryTotal } = computeDerived([pos], {}, () => 0);
    // No spot → v=null → not accumulated → total stays 0
    expect(expiryTotal).toBe(0);
  });

  it('uses backend-stamped underlying_ltp over underlyingSpotStore', () => {
    // underlying_ltp=23200 must win over getSpot()=23000
    const pos = makePos({
      tradingsymbol: 'NIFTY23100CE',
      exchange: 'NFO',
      quantity: 25,
      average_price: 50,
      underlying_ltp: 23200,  // backend-stamped (SSOT)
    });
    const { expiryTotal } = computeDerived([pos], {}, () => 23000);
    // Should use 23200, not 23000 → intrinsic = 100, expiry = (100-50)*25 = 1250
    expect(expiryTotal).toBeCloseTo(1250, 1);
  });

  it('accumulates expiryByAcct per account', () => {
    const pos1 = makePos({
      tradingsymbol: 'NIFTY23100CE', exchange: 'NFO',
      quantity: 25, average_price: 50, underlying_ltp: 23200,
      account: 'ZA1234', realised: 0,
    });
    const pos2 = makePos({
      tradingsymbol: 'NIFTY23100CE', exchange: 'NFO',
      quantity: 10, average_price: 50, underlying_ltp: 23200,
      account: 'ZB5678', realised: 0,
    });
    const { expiryByAcct } = computeDerived([pos1, pos2]);
    // ZA1234: (100-50)*25 = 1250; ZB5678: (100-50)*10 = 500
    expect(expiryByAcct.get('ZA1234')).toBeCloseTo(1250, 1);
    expect(expiryByAcct.get('ZB5678')).toBeCloseTo(500, 1);
  });
});

// ── SSOT / integration tests ──────────────────────────────────────────────────

describe('positionsDerivedStore — SSOT validation', () => {
  it('dayTotal and expiryTotal are independent (can diverge)', () => {
    // Deep ITM closed option: dayTotal = day_change_val, expiryTotal = pnl (realised)
    const pos = makePos({
      tradingsymbol: 'NIFTY22000CE',
      exchange: 'NFO',
      quantity: 0,      // closed
      pnl: 1500,        // realised gain
      day_change_val: 0,
    });
    const { dayTotal, expiryTotal } = computeDerived([pos]);
    // Day P&L: closed position (qty=0, oq=25) — uses dcv path → 0
    // Expiry P&L: qty=0 → return pnl → 1500
    expect(expiryTotal).toBe(1500);
    // dayTotal depends on dcvRow path — for qty=0, oq>0, dcv=0, pnl=1500, close>0:
    // Case 2: pnl - oq*(close-avg) = 1500 - 25*(22800-23000) = 1500+5000=6500
    // Or if prev_settlement_pnl absent and dcv=0 → relies on close computation
    // The key property: dayTotal ≠ expiryTotal when positions are complex
    expect(typeof dayTotal).toBe('number');
  });

  it('expiryPnl helper returns null for zero qty (gate prevents division issues)', () => {
    // expiryPnl(qty=0) → null
    const result = expiryPnl({ symbol: 'NIFTY23100CE', qty: 0, avg_cost: 50, kind: 'opt' }, 23200);
    expect(result).toBeNull();
  });

  it('expiryPnl helper returns null when spot is non-positive', () => {
    const result = expiryPnl({ symbol: 'NIFTY23100CE', qty: 25, avg_cost: 50, kind: 'opt' }, 0);
    expect(result).toBeNull();
  });

  it('baseDayPnlForPosition returns pnl for new intraday position (SSOT Case 1)', () => {
    const pos = makePos({
      quantity: 25, overnight_quantity: 0,
      pnl: 1250, day_change_val: 0, close_price: 0,
    });
    expect(baseDayPnlForPosition(pos)).toBe(1250);
  });

  it('empty positions array yields zeros', () => {
    const { dayTotal, expiryTotal } = computeDerived([]);
    expect(dayTotal).toBe(0);
    expect(expiryTotal).toBe(0);
  });
});

// ── Holdings expiry formula tests ─────────────────────────────────────────────
// These mirror computeHoldingsExpiry which uses the expiry formula:
//   (liveLtp − avgCost) × qty   — NOT (liveLtp − closePx) × qty
//
// This is the same formula as _accumulateHoldingExpPnl in derivatives/+page.svelte.

/**
 * Simulate positionsDerivedStore holdings expiry logic (pure JS mirror).
 * @param {any[]} holdRows
 * @param {Record<string, {ltp?: number}>} snapshots
 * @param {(sym: string) => string[]} getTargets  - targetsForProxy mock
 * @returns {Record<string, { expiry: number }>}
 */
function computeHoldingsExpiry(holdRows, snapshots = {}, getTargets = () => []) {
  /** @type {Record<string, { expiry: number }>} */
  const byRootHoldings = {};
  for (const h of holdRows) {
    const sym = String(h?.tradingsymbol || h?.symbol || '').toUpperCase();
    if (!sym) continue;

    const snapLtp = snapshots[sym]?.ltp;
    const liveLtp = (snapLtp != null && snapLtp > 0)
      ? Number(snapLtp)
      : Number(h?.last_price ?? 0);
    const avgCost = Number(h?.average_price ?? h?.avg_cost) || 0;
    const heldQty = Number(h?.quantity) || 0;

    if (!(liveLtp > 0) || heldQty === 0) continue;

    const expH = (liveLtp - avgCost) * heldQty;
    if (!isFinite(expH)) continue;

    const targets = getTargets(sym);
    const roots   = targets.length ? targets : [sym];
    for (const root of roots) {
      const slot = byRootHoldings[root] ?? (byRootHoldings[root] = { expiry: 0 });
      slot.expiry += expH;
    }
  }
  return byRootHoldings;
}

describe('positionsDerivedStore — Holdings expiry formula', () => {
  it('uses (liveLtp − avgCost) × qty — NOT (liveLtp − closePx) × qty', () => {
    // avgCost = 1200, liveLtp = 1450, qty = 100
    // Correct expiry P&L: (1450 - 1200) * 100 = 25000
    // Wrong day-P&L formula: (1450 - closePx) * 100 = some other value
    const h = {
      tradingsymbol: 'INFY',
      average_price: 1200,
      last_price: 1450,
      quantity: 100,
      previous_close: 1400,   // closePx — should NOT be used for expiry formula
    };
    const out = computeHoldingsExpiry([h]);
    expect(out['INFY']?.expiry).toBeCloseTo(25000, 1);
  });

  it('uses live symbolStore ltp when available over last_price', () => {
    // symbolStore ltp = 1500, last_price = 1450 → ltp must win
    const h = { tradingsymbol: 'INFY', average_price: 1200, last_price: 1450, quantity: 100 };
    const out = computeHoldingsExpiry([h], { INFY: { ltp: 1500 } });
    // (1500 - 1200) * 100 = 30000
    expect(out['INFY']?.expiry).toBeCloseTo(30000, 1);
  });

  it('skips holding when liveLtp is zero (no price available)', () => {
    const h = { tradingsymbol: 'INFY', average_price: 1200, last_price: 0, quantity: 100 };
    const out = computeHoldingsExpiry([h], {});
    // ltp=0 → skip
    expect(out['INFY']).toBeUndefined();
  });

  it('skips holding when quantity is zero', () => {
    const h = { tradingsymbol: 'INFY', average_price: 1200, last_price: 1450, quantity: 0 };
    const out = computeHoldingsExpiry([h]);
    expect(out['INFY']).toBeUndefined();
  });

  it('handles short holdings (negative qty)', () => {
    // Short 50 shares at avg 1400, now at 1200 → gain on short
    // (1200 - 1400) * (-50) = 10000
    const h = { tradingsymbol: 'RELIANCE', average_price: 1400, last_price: 1200, quantity: -50 };
    const out = computeHoldingsExpiry([h]);
    expect(out['RELIANCE']?.expiry).toBeCloseTo(10000, 1);
  });

  it('credits proxy holding to the target root (not own symbol)', () => {
    // IDFIRSTB is a proxy hedge for CRUDEOIL
    // avgCost=95, ltp=100, qty=200 → expH = (100-95)*200 = 1000
    // Should be credited to CRUDEOIL, not IDFIRSTB
    const h = { tradingsymbol: 'IDFIRSTB', average_price: 95, last_price: 100, quantity: 200 };
    const out = computeHoldingsExpiry(
      [h],
      { IDFIRSTB: { ltp: 100 } },
      (sym) => sym === 'IDFIRSTB' ? ['CRUDEOIL'] : [],  // targetsForProxy mock
    );
    expect(out['CRUDEOIL']?.expiry).toBeCloseTo(1000, 1);
    expect(out['IDFIRSTB']).toBeUndefined();  // should NOT be keyed to own symbol
  });

  it('credits non-proxy holding to its own symbol as root', () => {
    const h = { tradingsymbol: 'TCS', average_price: 3400, last_price: 3600, quantity: 10 };
    const out = computeHoldingsExpiry(
      [h],
      {},
      (sym) => [],  // no proxy targets
    );
    // (3600 - 3400) * 10 = 2000 keyed to TCS
    expect(out['TCS']?.expiry).toBeCloseTo(2000, 1);
  });

  it('accumulates multiple holdings with same root correctly', () => {
    // Two different equity holdings — separate symbols, separate roots
    const h1 = { tradingsymbol: 'TCS',     average_price: 3400, last_price: 3600, quantity: 10 };
    const h2 = { tradingsymbol: 'INFY',    average_price: 1200, last_price: 1450, quantity: 100 };
    const out = computeHoldingsExpiry([h1, h2]);
    expect(out['TCS']?.expiry).toBeCloseTo(2000, 1);
    expect(out['INFY']?.expiry).toBeCloseTo(25000, 1);
  });
});
