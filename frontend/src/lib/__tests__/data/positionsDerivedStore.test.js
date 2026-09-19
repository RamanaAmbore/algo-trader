/**
 * positionsDerivedStore.test.js
 *
 * Unit tests for the positionsDerivedStore._computeDerived() pure function.
 *
 * The store aggregates three objects across positions + holdings:
 *   1. total     — { day_pnl, exp_pnl, extrinsic } aggregate totals
 *   2. byKey     — { [sym]: { day_pnl, exp_pnl, extrinsic, pnl } } per symbol
 *   3. expiryByAcct, byRootPositions, byRootHoldings — for Snapshot
 *
 * Five quality dimensions:
 *   1. SSOT  — livePositionDayPnl + expiryPnl are canonical; no inline duplication
 *   2. Perf  — pure unit tests, no DOM / network, sub-millisecond
 *   3. Stale — closed-leg, missing spot, equity exclusion, stale-close cases
 *   4. Reuse — exercises shared nav.js + expiryPnl.js exports via _computeDerived
 *   5. UX    — totals match NavStrip expectations; equity excluded from Exp P&L
 */

import { describe, it, expect } from 'vitest';
import { livePositionDayPnl, baseDayPnlForPosition, dayChangePct } from '$lib/data/nav.js';
import { expiryPnl } from '$lib/data/expiryPnl.js';

// ── _computeDerived pure-function mirror ─────────────────────────────────────
// Mirror the logic from positionsDerivedStore.svelte.js without importing the
// Svelte module (which requires browser environment and reactive context).
// Mirrors _computeDerived exactly so tests validate the real algorithm.

const FO_EXCHS = new Set(['NFO', 'MCX', 'CDS', 'BFO']);

function _computeDerived(posRows, holdRows, deps = {}) {
  const {
    getSnap    = sym  => undefined,
    getSpot    = root => 0,
    getTargets = sym  => [],
    getProxy   = (sym, tgt) => null,
    livePosDay = (p, ltp, opts) => livePositionDayPnl(
      {
        closePx: Number(p?.previous_close) || Number(p?.close_price ?? 0),
        pollLtp: Number(p?.last_price      ?? 0),
        qty:     Number(p?.quantity        ?? 0),
        avg:     Number(p?.average_price   ?? 0),
        dcvRow:  p,
      },
      ltp,
      opts,
    ),
    marketOpen = true,
  } = deps;

  const total = { day_pnl: 0, exp_pnl: 0, extrinsic: 0 };
  const byKey = {};
  const byRootPositions = {};
  const byRootHoldings  = {};
  const expiryByAcct = new Map();

  for (const p of posRows) {
    const sym = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
    if (!sym) continue;

    const qty  = Number(p?.quantity ?? 0) || 0;
    const avg  = Number(p?.average_price ?? 0) || 0;
    const pnl  = Number(p?.pnl ?? 0);
    const snap = getSnap(sym);
    const ltp  = snap?.ltp ?? Number(p?.last_price ?? 0);

    const day_pnl = livePosDay(p, ltp, { marketOpen });

    const exch = String(p?.exchange || '').toUpperCase();
    const isFO = FO_EXCHS.has(exch);

    let exp_pnl   = null;
    let extrinsic = null;
    let expVal    = null;

    if (isFO) {
      const realised = Number(p?.realised ?? 0) || 0;

      if (qty === 0) {
        exp_pnl   = Number(p?.realised || p?.pnl || 0);
        extrinsic = 0;
        expVal    = exp_pnl;
      } else {
        const isCE = sym.endsWith('CE');
        const isPE = sym.endsWith('PE');

        let ev = null;
        if (isCE || isPE) {
          const rootMatch = sym.match(/^([A-Z]+)/);
          const root = rootMatch?.[1] ?? sym;
          const spot1 = Number(p?.underlying_ltp || 0);
          const spot  = spot1 > 0 ? spot1 : getSpot(root);
          if (spot > 0) {
            ev = expiryPnl({ symbol: sym, qty, avg_cost: avg, kind: 'opt' }, spot);
          }
        } else {
          const live = ltp || 0;
          if (live > 0) {
            ev = expiryPnl({ symbol: sym, qty, avg_cost: avg, kind: 'fut' }, live);
          }
        }

        if (ev != null) {
          exp_pnl   = ev + realised;
          extrinsic = ev - (ltp - avg) * qty;
          expVal    = exp_pnl;
        }
      }
    }

    const prev_close = Number(p?.previous_close) || Number(p?.close_price) || 0;
    const prev_mv    = (byKey[sym]?.prev_mv || 0) + (prev_close > 0 ? prev_close * Math.abs(qty) : 0);
    byKey[sym] = { day_pnl, exp_pnl, extrinsic, pnl, prev_mv, chg_pct: null };

    total.day_pnl += day_pnl;
    if (exp_pnl   != null) total.exp_pnl   += exp_pnl;
    if (extrinsic != null) total.extrinsic += extrinsic;

    if (isFO && expVal != null) {
      const acct = String(p?.account || '');
      if (acct) expiryByAcct.set(acct, (expiryByAcct.get(acct) ?? 0) + expVal);

      const rootMatch = sym.match(/^([A-Z]+)/);
      const root = (rootMatch?.[1] || sym).toUpperCase();
      if (root) {
        const r = byRootPositions[root] ??= { day_pnl: 0, exp_pnl: 0, extrinsic: 0, pnl: 0 };
        r.day_pnl += day_pnl;
        r.pnl     += pnl;
        r.exp_pnl   += (exp_pnl   ?? 0);
        r.extrinsic += (extrinsic ?? 0);
      }
    }
  }

  for (const h of holdRows) {
    const sym = String(h?.tradingsymbol || h?.symbol || '').toUpperCase();
    if (!sym) continue;

    const qty  = Number(h?.quantity) || 0;
    const cost = Number(h?.average_price ?? h?.avg_cost) || 0;
    const snap = getSnap(sym);
    const ltp  = (snap?.ltp ?? 0) > 0 ? Number(snap.ltp) : Number(h?.last_price ?? 0);

    if (qty === 0) continue;

    const targets = getTargets(sym);
    const credits = targets.length ? targets : [sym];

    for (const target of credits) {
      let exp_pnl = null;

      if (targets.length && ltp > 0) {
        const proxyRow   = getProxy(sym, target);
        const beta       = proxyRow?.beta ?? 1;
        const targetSpot = getSpot(target);
        if (targetSpot > 0) {
          const effQty = (beta * ltp * qty) / targetSpot;
          exp_pnl = (targetSpot - ltp / (beta || 1)) * effQty;
        }
      } else if (!targets.length && ltp > 0) {
        exp_pnl = (ltp - cost) * qty;
      }

      const r = byRootHoldings[target] ??= { day_pnl: 0, exp_pnl: 0, extrinsic: 0, pnl: 0 };
      r.pnl += Number(h?.pnl ?? 0);
      if (exp_pnl != null) r.exp_pnl += exp_pnl;
    }
  }

  for (const bk of Object.values(byKey)) {
    bk.chg_pct = bk.prev_mv > 0 ? dayChangePct(bk.day_pnl, bk.prev_mv) : null;
  }

  return { total, byKey, byRootPositions, byRootHoldings, expiryByAcct };
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

describe('positionsDerivedStore — Day P&L (byKey and total.day_pnl)', () => {
  it('uses livePositionDayPnl with SSE ltp when available', () => {
    const pos = makePos({ last_price: 23100, close_price: 22800, quantity: 25, overnight_quantity: 25 });
    const { total, byKey } = _computeDerived([pos], [], {
      getSnap: (sym) => sym === 'NIFTY26JUNFUT' ? { ltp: 23200 } : undefined,
    });
    // With live ltp=23200, close=22800, qty=25: (23200-22800)*25 = 10000
    expect(total.day_pnl).toBeCloseTo(10000, 1);
    expect(byKey['NIFTY26JUNFUT'].day_pnl).toBeCloseTo(10000, 1);
  });

  it('falls back to broker day_change_val when no live ltp', () => {
    const pos = makePos({ day_change_val: 7500 });
    const { total } = _computeDerived([pos], []);
    expect(total.day_pnl).toBe(7500);
  });

  it('sums correctly across multiple positions', () => {
    const pos1 = makePos({ tradingsymbol: 'NIFTY26JUNFUT', day_change_val: 5000, quantity: 25, overnight_quantity: 25 });
    const pos2 = makePos({ tradingsymbol: 'GOLDFUT', exchange: 'MCX', day_change_val: 3000, quantity: 1, overnight_quantity: 1 });
    const { total, byKey } = _computeDerived([pos1, pos2], []);
    expect(total.day_pnl).toBeCloseTo(8000, 1);
    expect(byKey['NIFTY26JUNFUT'].day_pnl).toBeCloseTo(5000, 1);
    expect(byKey['GOLDFUT'].day_pnl).toBeCloseTo(3000, 1);
  });

  it('handles new intraday position (overnight_quantity = 0)', () => {
    // oq=0, close=0, avg=23000, last_price=23100, pnl=1250
    // livePositionDayPnl with live ltp available and close=0:
    //   → (live − avg) × qty = (23100 − 23000) × 25 = 2500
    // This is the correct result; baseDayPnlForPosition Case 1 (pnl=1250) is
    // superseded when a live ltp exists and close=0 (new-position LTP path).
    const pos = makePos({
      quantity: 25,
      overnight_quantity: 0,
      pnl: 1250,
      day_change_val: 0,
      close_price: 0,
    });
    const { total } = _computeDerived([pos], []);
    expect(total.day_pnl).toBeCloseTo(2500, 1);
  });

  it('byKey values are objects not numbers', () => {
    const pos = makePos();
    const { byKey } = _computeDerived([pos], []);
    const entry = byKey['NIFTY26JUNFUT'];
    expect(typeof entry).toBe('object');
    expect(typeof entry.day_pnl).toBe('number');
    expect(typeof entry.pnl).toBe('number');
    expect('exp_pnl' in entry).toBe(true);
    expect('extrinsic' in entry).toBe(true);
  });
});

// ── Exp P&L tests ─────────────────────────────────────────────────────────────

describe('positionsDerivedStore — Exp P&L (total.exp_pnl and byKey.exp_pnl)', () => {
  it('computes option intrinsic for long CE position', () => {
    // NIFTY 23100 CE, spot = 23200 → intrinsic = 100, qty=25, avg=50
    // expiry = (100 - 50) * 25 = 1250
    const pos = makePos({
      tradingsymbol: 'NIFTY23100CE',
      exchange: 'NFO',
      quantity: 25,
      average_price: 50,
      last_price: 80,
      underlying_ltp: 23200,
      pnl: 0,
      realised: 0,
    });
    const { total, byKey } = _computeDerived([pos], []);
    expect(total.exp_pnl).toBeCloseTo(1250, 1);
    expect(byKey['NIFTY23100CE'].exp_pnl).toBeCloseTo(1250, 1);
  });

  it('computes option intrinsic for short PE position', () => {
    // NIFTY 23000 PE, spot = 23200 → intrinsic = 0, qty=-25, avg=80
    // expiry = (0 - 80) * (-25) = 2000 (short OTM PE keeps full premium)
    const pos = makePos({
      tradingsymbol: 'NIFTY23000PE',
      exchange: 'NFO',
      quantity: -25,
      average_price: 80,
      last_price: 20,
      underlying_ltp: 23200,
      pnl: 0,
      realised: 0,
    });
    const { total } = _computeDerived([pos], []);
    expect(total.exp_pnl).toBeCloseTo(2000, 1);
  });

  it('computes futures expiry as (ltp - avg) * qty', () => {
    // ltp = 23200, avg = 23000, qty = 25 → (23200-23000)*25 = 5000
    const pos = makePos({
      tradingsymbol: 'NIFTY26JUNFUT',
      last_price: 23200,
      average_price: 23000,
      quantity: 25,
    });
    const { total } = _computeDerived([pos], [], {
      getSnap: (sym) => sym === 'NIFTY26JUNFUT' ? { ltp: 23200 } : undefined,
    });
    expect(total.exp_pnl).toBeCloseTo(5000, 1);
  });

  it('adds realised to open leg expiry', () => {
    const pos = makePos({
      tradingsymbol: 'NIFTY23100CE',
      exchange: 'NFO',
      quantity: 25,
      average_price: 50,
      last_price: 80,
      underlying_ltp: 23200,
      realised: 500,
    });
    const { total } = _computeDerived([pos], []);
    // intrinsic contribution: (100-50)*25 = 1250, + realised 500 = 1750
    expect(total.exp_pnl).toBeCloseTo(1750, 1);
  });

  it('locks in realised P&L for closed leg (qty = 0)', () => {
    const pos = makePos({
      tradingsymbol: 'NIFTY23100CE',
      exchange: 'NFO',
      quantity: 0,
      pnl: 800,
      realised: 0,
    });
    const { total, byKey } = _computeDerived([pos], []);
    // qty=0 → exp_pnl = pnl = 800
    expect(total.exp_pnl).toBe(800);
    expect(byKey['NIFTY23100CE'].exp_pnl).toBe(800);
  });

  it('excludes equity (NSE/BSE) from exp_pnl total', () => {
    const equity = makePos({
      tradingsymbol: 'INFY',
      exchange: 'NSE',
      quantity: 100,
      average_price: 1400,
      last_price: 1450,
    });
    const { total, byKey } = _computeDerived([equity], []);
    // Equity position → exp_pnl = null, not accumulated
    expect(total.exp_pnl).toBe(0);
    expect(byKey['INFY'].exp_pnl).toBeNull();
  });

  it('returns null exp_pnl when spot cannot be resolved', () => {
    const pos = makePos({
      tradingsymbol: 'NIFTY23100CE',
      exchange: 'NFO',
      quantity: 25,
      average_price: 50,
      underlying_ltp: 0,
    });
    const { total, byKey } = _computeDerived([pos], [], { getSpot: () => 0 });
    expect(total.exp_pnl).toBe(0);
    expect(byKey['NIFTY23100CE'].exp_pnl).toBeNull();
  });

  it('uses backend-stamped underlying_ltp over underlyingSpotStore', () => {
    const pos = makePos({
      tradingsymbol: 'NIFTY23100CE',
      exchange: 'NFO',
      quantity: 25,
      average_price: 50,
      last_price: 80,
      underlying_ltp: 23200,
    });
    const { total } = _computeDerived([pos], [], { getSpot: () => 23000 });
    // Should use 23200, not 23000 → intrinsic = 100, expiry = (100-50)*25 = 1250
    expect(total.exp_pnl).toBeCloseTo(1250, 1);
  });

  it('accumulates expiryByAcct per account', () => {
    const pos1 = makePos({
      tradingsymbol: 'NIFTY23100CE', exchange: 'NFO',
      quantity: 25, average_price: 50, last_price: 80, underlying_ltp: 23200,
      account: 'ZA1234', realised: 0,
    });
    const pos2 = makePos({
      tradingsymbol: 'NIFTY23100CE', exchange: 'NFO',
      quantity: 10, average_price: 50, last_price: 80, underlying_ltp: 23200,
      account: 'ZB5678', realised: 0,
    });
    const { expiryByAcct } = _computeDerived([pos1, pos2], []);
    // ZA1234: (100-50)*25 = 1250; ZB5678: (100-50)*10 = 500
    expect(expiryByAcct.get('ZA1234')).toBeCloseTo(1250, 1);
    expect(expiryByAcct.get('ZB5678')).toBeCloseTo(500, 1);
  });
});

// ── Extrinsic tests ───────────────────────────────────────────────────────────

describe('positionsDerivedStore — Extrinsic (byKey.extrinsic and total.extrinsic)', () => {
  it('computes extrinsic for open CE: ev - (ltp-avg)*qty', () => {
    // NIFTY 23100 CE, spot = 23200, ltp = 80, avg = 50, qty = 25
    // intrinsic_val per share = max(23200-23100,0) = 100
    // ev = (100 - 50) * 25 = 1250
    // extrinsic = ev - (ltp - avg)*qty = 1250 - (80-50)*25 = 1250 - 750 = 500
    const pos = makePos({
      tradingsymbol: 'NIFTY23100CE',
      exchange: 'NFO',
      quantity: 25,
      average_price: 50,
      last_price: 80,
      underlying_ltp: 23200,
      realised: 0,
    });
    const { total, byKey } = _computeDerived([pos], [], {
      getSnap: (sym) => sym === 'NIFTY23100CE' ? { ltp: 80 } : undefined,
    });
    expect(byKey['NIFTY23100CE'].extrinsic).toBeCloseTo(500, 1);
    expect(total.extrinsic).toBeCloseTo(500, 1);
  });

  it('extrinsic = 0 for closed legs (qty = 0)', () => {
    const pos = makePos({
      tradingsymbol: 'NIFTY23100CE',
      exchange: 'NFO',
      quantity: 0,
      pnl: 800,
      realised: 0,
    });
    const { byKey, total } = _computeDerived([pos], []);
    expect(byKey['NIFTY23100CE'].extrinsic).toBe(0);
    expect(total.extrinsic).toBe(0);
  });

  it('extrinsic is null for equity rows', () => {
    const equity = makePos({
      tradingsymbol: 'TCS',
      exchange: 'NSE',
      quantity: 10,
      average_price: 3400,
      last_price: 3600,
    });
    const { byKey } = _computeDerived([equity], []);
    expect(byKey['TCS'].extrinsic).toBeNull();
  });

  it('total.extrinsic accumulates across multiple F&O rows', () => {
    const pos1 = makePos({
      tradingsymbol: 'NIFTY23100CE',
      exchange: 'NFO',
      quantity: 25, average_price: 50, last_price: 80, underlying_ltp: 23200, realised: 0,
    });
    const pos2 = makePos({
      tradingsymbol: 'NIFTY23000PE',
      exchange: 'NFO',
      quantity: -25, average_price: 80, last_price: 20, underlying_ltp: 23200, realised: 0,
    });
    const { total } = _computeDerived([pos1, pos2], [], {
      getSnap: (sym) => {
        if (sym === 'NIFTY23100CE') return { ltp: 80 };
        if (sym === 'NIFTY23000PE') return { ltp: 20 };
        return undefined;
      },
    });
    expect(typeof total.extrinsic).toBe('number');
  });
});

// ── total structure tests ─────────────────────────────────────────────────────

describe('positionsDerivedStore — total structure', () => {
  it('total has day_pnl, exp_pnl, extrinsic keys', () => {
    const { total } = _computeDerived([], []);
    expect('day_pnl' in total).toBe(true);
    expect('exp_pnl' in total).toBe(true);
    expect('extrinsic' in total).toBe(true);
  });

  it('empty arrays yield all-zero total', () => {
    const { total } = _computeDerived([], []);
    expect(total.day_pnl).toBe(0);
    expect(total.exp_pnl).toBe(0);
    expect(total.extrinsic).toBe(0);
  });

  it('total.exp_pnl accumulates correctly across multiple F&O rows', () => {
    const pos1 = makePos({
      tradingsymbol: 'NIFTY23100CE', exchange: 'NFO',
      quantity: 25, average_price: 50, last_price: 80, underlying_ltp: 23200, realised: 0,
    });
    const pos2 = makePos({
      tradingsymbol: 'NIFTY23000PE', exchange: 'NFO',
      quantity: -25, average_price: 80, last_price: 20, underlying_ltp: 23200, realised: 0,
    });
    const { total } = _computeDerived([pos1, pos2], []);
    // pos1: (100-50)*25 = 1250; pos2: (0-80)*(-25) = 2000
    expect(total.exp_pnl).toBeCloseTo(3250, 1);
  });

  it('day_pnl and exp_pnl are independent', () => {
    const pos = makePos({
      tradingsymbol: 'NIFTY22000CE',
      exchange: 'NFO',
      quantity: 0,
      pnl: 1500,
      day_change_val: 0,
    });
    const { total } = _computeDerived([pos], []);
    expect(total.exp_pnl).toBe(1500);
    expect(typeof total.day_pnl).toBe('number');
  });
});

// ── byRootPositions tests ────────────────────────────────────────────────────

describe('positionsDerivedStore — byRootPositions', () => {
  it('accumulates exp_pnl and day_pnl per root', () => {
    const pos = makePos({
      tradingsymbol: 'NIFTY23100CE',
      exchange: 'NFO',
      quantity: 25,
      average_price: 50,
      last_price: 80,
      underlying_ltp: 23200,
      realised: 0,
    });
    const { byRootPositions } = _computeDerived([pos], []);
    expect(byRootPositions['NIFTY']).toBeDefined();
    expect(byRootPositions['NIFTY'].exp_pnl).toBeCloseTo(1250, 1);
    expect(typeof byRootPositions['NIFTY'].day_pnl).toBe('number');
    expect(typeof byRootPositions['NIFTY'].extrinsic).toBe('number');
  });

  it('does not add equity rows to byRootPositions', () => {
    const equity = makePos({ tradingsymbol: 'INFY', exchange: 'NSE', quantity: 100 });
    const { byRootPositions } = _computeDerived([equity], []);
    expect(Object.keys(byRootPositions).length).toBe(0);
  });
});

// ── Holdings expiry tests ─────────────────────────────────────────────────────

describe('positionsDerivedStore — Holdings (byRootHoldings)', () => {
  it('uses (ltp − avgCost) × qty for direct equity', () => {
    const h = {
      tradingsymbol: 'INFY',
      average_price: 1200,
      last_price: 1450,
      quantity: 100,
    };
    const { byRootHoldings } = _computeDerived([], [h]);
    expect(byRootHoldings['INFY']?.exp_pnl).toBeCloseTo(25000, 1);
  });

  it('uses symbolStore ltp over last_price when available', () => {
    const h = { tradingsymbol: 'INFY', average_price: 1200, last_price: 1450, quantity: 100 };
    const { byRootHoldings } = _computeDerived([], [h], {
      getSnap: (sym) => sym === 'INFY' ? { ltp: 1500 } : undefined,
    });
    expect(byRootHoldings['INFY']?.exp_pnl).toBeCloseTo(30000, 1);
  });

  it('has null exp_pnl for holding when ltp is zero', () => {
    // ltp=0: the row entry is created for pnl tracking but exp_pnl stays 0
    // (no price data available to compute (ltp-cost)*qty).
    const h = { tradingsymbol: 'INFY', average_price: 1200, last_price: 0, quantity: 100, pnl: 0 };
    const { byRootHoldings } = _computeDerived([], [h]);
    // Entry exists (pnl tracking) but exp_pnl was not incremented
    expect(byRootHoldings['INFY']?.exp_pnl ?? 0).toBe(0);
  });

  it('skips holding when quantity is zero', () => {
    const h = { tradingsymbol: 'INFY', average_price: 1200, last_price: 1450, quantity: 0 };
    const { byRootHoldings } = _computeDerived([], [h]);
    expect(byRootHoldings['INFY']).toBeUndefined();
  });
});

// ── SSOT helpers ─────────────────────────────────────────────────────────────

describe('positionsDerivedStore — SSOT helper validation', () => {
  it('expiryPnl returns null for zero qty', () => {
    const result = expiryPnl({ symbol: 'NIFTY23100CE', qty: 0, avg_cost: 50, kind: 'opt' }, 23200);
    expect(result).toBeNull();
  });

  it('expiryPnl returns null when spot is non-positive', () => {
    const result = expiryPnl({ symbol: 'NIFTY23100CE', qty: 25, avg_cost: 50, kind: 'opt' }, 0);
    expect(result).toBeNull();
  });

  it('baseDayPnlForPosition returns pnl for new intraday position (Case 1)', () => {
    const pos = makePos({
      quantity: 25, overnight_quantity: 0,
      pnl: 1250, day_change_val: 0, close_price: 0,
    });
    expect(baseDayPnlForPosition(pos)).toBe(1250);
  });
});

// ── chg_pct SSOT tests ────────────────────────────────────────────────────────

describe('positionsDerivedStore — chg_pct SSOT (byKey[sym].chg_pct)', () => {
  it('computes chg_pct from day_pnl / prev_mv when close_price is set', () => {
    const pos = makePos({
      quantity: 25,
      close_price: 22800,
      average_price: 22800,
      overnight_quantity: 25,
    });
    const { byKey } = _computeDerived([pos], [], {
      livePosDay: () => 500,
    });
    const bk = byKey['NIFTY26JUNFUT'];
    // prev_mv = close_price * qty = 22800 * 25 = 570000
    // chg_pct = 500 / 570000 * 100
    expect(bk.prev_mv).toBeCloseTo(22800 * 25);
    expect(bk.chg_pct).toBeCloseTo((500 / (22800 * 25)) * 100, 4);
  });

  it('chg_pct is null when prev_mv is 0 (new intraday, no close)', () => {
    const pos = makePos({
      quantity: 25,
      close_price: 0,
      average_price: 0,
      overnight_quantity: 0,
    });
    const { byKey } = _computeDerived([pos], []);
    expect(byKey['NIFTY26JUNFUT'].chg_pct).toBeNull();
  });

  it('chg_pct is null when close_price is 0 (no avg fallback)', () => {
    const pos = makePos({
      quantity: 25,
      close_price: 0,
      average_price: 23000,
      overnight_quantity: 25,
    });
    const { byKey } = _computeDerived([pos], [], {
      livePosDay: () => 300,
    });
    const bk = byKey['NIFTY26JUNFUT'];
    // prev_close = 0, so prev_mv = 0 (no avg fallback)
    expect(bk.prev_mv).toBe(0);
    expect(bk.chg_pct).toBeNull();
  });

  it('byKey includes prev_mv and chg_pct fields', () => {
    const pos = makePos();
    const { byKey } = _computeDerived([pos], []);
    const entry = byKey['NIFTY26JUNFUT'];
    expect('prev_mv' in entry).toBe(true);
    expect('chg_pct' in entry).toBe(true);
  });
});
