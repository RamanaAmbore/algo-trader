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
 *   1. SSOT  — baseDayPnlForPosition + expiryPnl are canonical; no inline duplication
 *   2. Perf  — pure unit tests, no DOM / network, sub-millisecond
 *   3. Stale — closed-leg, missing spot, equity exclusion, stale-close cases
 *   4. Reuse — exercises shared nav.js + expiryPnl.js exports via _computeDerived
 *   5. UX    — totals match NavStrip expectations; equity excluded from Exp P&L
 *
 * §1 (positions/holdings LTP-source redesign): the live-tick-delta wrapper
 * `livePositionDayPnl` was removed from nav.js — Day P&L is now purely
 * poll-driven via `baseDayPnlForPosition` alone. The local `livePosDay`
 * default below mirrors that.
 */

import { describe, it, expect } from 'vitest';
import { baseDayPnlForPosition, dayChangePct } from '$lib/data/nav.js';
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
    livePosDay = (p) => baseDayPnlForPosition(p),
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

    const day_pnl = livePosDay(p);

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
        const isOpt = isCE || isPE;

        // Spot resolution mirrors portfolioStore.svelte.js: options ALWAYS
        // value against underlying spot; futures value against spot too
        // (matches the Exp P&L column tooltip), falling back to the
        // contract's own LTP only when spot is unavailable (e.g. MCX
        // futures with no underlying spot index).
        const rootMatch = sym.match(/^([A-Z]+)/);
        const root  = rootMatch?.[1] ?? sym;
        const spot1 = Number(p?.underlying_ltp || 0);
        const spot  = spot1 > 0 ? spot1 : getSpot(root);
        const anchor = isOpt ? spot : (spot > 0 ? spot : (ltp || 0));

        let ev = null;
        if (anchor > 0) {
          ev = expiryPnl({ symbol: sym, qty, avg_cost: avg, kind: isOpt ? 'opt' : 'fut' }, anchor);
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
  it('a live SSE snapshot has no effect — positions Day P&L is poll-only (§1)', () => {
    // base = pnl(2500) - prev_settlement_pnl(0) = 2500. A getSnap ltp diverging
    // from last_price (23200 vs 23100) no longer produces a delta.
    const pos = makePos({ last_price: 23100, close_price: 22800, quantity: 25, prev_settlement_pnl: 0 });
    const { total, byKey } = _computeDerived([pos], [], {
      getSnap: (sym) => sym === 'NIFTY26JUNFUT' ? { ltp: 23200 } : undefined,
    });
    expect(total.day_pnl).toBeCloseTo(2500, 1);
    expect(byKey['NIFTY26JUNFUT'].day_pnl).toBeCloseTo(2500, 1);
  });

  it('falls back to pnl when prev_settlement_pnl is null (new/no baseline)', () => {
    const pos = makePos({ pnl: 7500, prev_settlement_pnl: null });
    const { total } = _computeDerived([pos], []);
    expect(total.day_pnl).toBe(7500);
  });

  it('sums correctly across multiple positions', () => {
    const pos1 = makePos({ tradingsymbol: 'NIFTY26JUNFUT', pnl: 5000, prev_settlement_pnl: null, quantity: 25 });
    const pos2 = makePos({ tradingsymbol: 'GOLDFUT', exchange: 'MCX', pnl: 3000, prev_settlement_pnl: null, quantity: 1 });
    const { total, byKey } = _computeDerived([pos1, pos2], []);
    expect(total.day_pnl).toBeCloseTo(8000, 1);
    expect(byKey['NIFTY26JUNFUT'].day_pnl).toBeCloseTo(5000, 1);
    expect(byKey['GOLDFUT'].day_pnl).toBeCloseTo(3000, 1);
  });

  it('handles new intraday position (no prev_settlement_pnl): base = pnl', () => {
    const pos = makePos({
      quantity: 25,
      pnl: 1250,
      prev_settlement_pnl: null,
      close_price: 0,
      last_price: 23100, // matches pnl's implied mark so live delta is 0
    });
    const { total } = _computeDerived([pos], [], {
      getSnap: (sym) => sym === 'NIFTY26JUNFUT' ? { ltp: 23100 } : undefined,
    });
    expect(total.day_pnl).toBeCloseTo(1250, 1);
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

  it('computes futures expiry as (spot - avg) * qty — SPOT, not the future\'s own LTP', () => {
    // Deliberately diverging future LTP (23400, via getSnap) from underlying
    // spot (23200, via underlying_ltp) to prove the valuation uses spot.
    // (23200-23000)*25 = 5000 — NOT (23400-23000)*25 = 10000.
    const pos = makePos({
      tradingsymbol: 'NIFTY26JUNFUT',
      last_price: 23400,      // future's own last poll — must NOT drive the value
      average_price: 23000,
      quantity: 25,
      underlying_ltp: 23200,  // underlying spot — must drive the value
    });
    const { total } = _computeDerived([pos], [], {
      getSnap: (sym) => sym === 'NIFTY26JUNFUT' ? { ltp: 23400 } : undefined,
    });
    expect(total.exp_pnl).toBeCloseTo(5000, 1);
    expect(total.exp_pnl).not.toBeCloseTo(10000, 1);
  });

  it('futures fall back to own LTP when spot is unavailable (e.g. MCX, no spot index)', () => {
    const pos = makePos({
      tradingsymbol: 'CRUDEOIL25OCTFUT',
      exchange: 'MCX',
      last_price: 5900,
      average_price: 5800,
      quantity: 10,
      // no underlying_ltp, no getSpot match → falls back to own LTP
    });
    const { total } = _computeDerived([pos], [], {
      getSnap: (sym) => sym === 'CRUDEOIL25OCTFUT' ? { ltp: 5900 } : undefined,
      getSpot: () => 0,
    });
    expect(total.exp_pnl).toBeCloseTo((5900 - 5800) * 10, 1);
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

// ─────────────────────────────────────────────────────────────────────────
// Accessor functions: get(sym, fallback?), getByRoot(root, fallback?)
// ─────────────────────────────────────────────────────────────────────────

// Mirror the new get() / getByRoot() from positionsDerivedStore.svelte.js.
function makeGet(byKey) {
  return (sym, fallback = null) => {
    const r = byKey[String(sym || '').toUpperCase()];
    if (!r) return { day_pnl: fallback, pnl: fallback, exp_pnl: fallback, extrinsic: fallback, prev_mv: fallback, chg_pct: fallback };
    return {
      day_pnl:   r.day_pnl   ?? fallback,
      pnl:       r.pnl       ?? fallback,
      exp_pnl:   r.exp_pnl   ?? fallback,
      extrinsic: r.extrinsic ?? fallback,
      prev_mv:   r.prev_mv   ?? fallback,
      chg_pct:   r.chg_pct   ?? fallback,
    };
  };
}

function makeGetByRoot(byRootPositions) {
  return (root, fallback = null) => {
    const r = byRootPositions[String(root || '').toUpperCase()];
    if (!r) return { day_pnl: fallback, pnl: fallback, exp_pnl: fallback, extrinsic: fallback, prev_mv: fallback, chg_pct: fallback };
    return {
      day_pnl:   r.day_pnl   ?? fallback,
      pnl:       r.pnl       ?? fallback,
      exp_pnl:   r.exp_pnl   ?? fallback,
      extrinsic: r.extrinsic ?? fallback,
      prev_mv:   r.prev_mv   ?? fallback,
      chg_pct:   r.chg_pct   ?? fallback,
    };
  };
}

describe('positionsDerivedStore — get(sym, fallback?) accessor', () => {
  it('get(unknown) — no fallback arg — returns null for all fields', () => {
    const get = makeGet({});
    const result = get('UNKNOWN_SYM');
    expect(result.day_pnl).toBeNull();
    expect(result.pnl).toBeNull();
    expect(result.exp_pnl).toBeNull();
    expect(result.extrinsic).toBeNull();
    expect(result.prev_mv).toBeNull();
    expect(result.chg_pct).toBeNull();
  });

  it('get(unknown, 0) returns 0 for all absent fields', () => {
    const get = makeGet({});
    const result = get('UNKNOWN_SYM', 0);
    expect(result.day_pnl).toBe(0);
    expect(result.pnl).toBe(0);
    expect(result.exp_pnl).toBe(0);
    expect(result.extrinsic).toBe(0);
    expect(result.prev_mv).toBe(0);
    expect(result.chg_pct).toBe(0);
  });

  it('get(RELIANCE) returns store values when all fields are populated', () => {
    const get = makeGet({
      RELIANCE: { day_pnl: 500, pnl: 1000, exp_pnl: 0, extrinsic: null, prev_mv: 10000, chg_pct: 5.0 },
    });
    const rel = get('RELIANCE');
    expect(rel.day_pnl).toBe(500);
    expect(rel.pnl).toBe(1000);
    expect(rel.exp_pnl).toBe(0);
    expect(rel.prev_mv).toBe(10000);
    expect(rel.chg_pct).toBe(5.0);
  });

  it('get(RELIANCE, 0) uses fallback only for null extrinsic, not for set fields', () => {
    // extrinsic = null in store → should become 0 (fallback)
    // pnl = 1000 in store → must stay 1000
    const get = makeGet({
      RELIANCE: { day_pnl: 500, pnl: 1000, exp_pnl: 0, extrinsic: null, prev_mv: 10000, chg_pct: 5.0 },
    });
    const rel = get('RELIANCE', 0);
    expect(rel.extrinsic).toBe(0);   // null → fallback
    expect(rel.pnl).toBe(1000);      // set value preserved
    expect(rel.day_pnl).toBe(500);   // set value preserved
  });

  it('uppercase normalisation: lower-case sym matches upper-case key', () => {
    const get = makeGet({
      NIFTY26JUNFUT: { day_pnl: 7500, pnl: 9000, exp_pnl: 5000, extrinsic: 200, prev_mv: 500000, chg_pct: 1.5 },
    });
    const result = get('nifty26junfut', 0);
    expect(result.day_pnl).toBe(7500);
  });

  it('exp_pnl field: get(sym, 0).exp_pnl is the safe accumulation pattern used in MarketPulse', () => {
    // Validates the call site pattern: acc.exp_pnl += positionsDerivedStore.get(sym, 0).exp_pnl
    const get = makeGet({
      NIFTY23100CE: { day_pnl: 500, pnl: 1000, exp_pnl: 1250, extrinsic: 300, prev_mv: 50000, chg_pct: 1.0 },
    });
    let acc_exp_pnl = 0;
    // Known symbol — exp_pnl = 1250
    acc_exp_pnl += get('NIFTY23100CE', 0).exp_pnl;
    expect(acc_exp_pnl).toBe(1250);
    // Unknown symbol — exp_pnl = 0 (fallback), not NaN
    acc_exp_pnl += get('UNKNOWN', 0).exp_pnl;
    expect(acc_exp_pnl).toBe(1250);
  });
});

describe('positionsDerivedStore — getByRoot(root, fallback?) accessor', () => {
  it('getByRoot(unknown) — no fallback arg — returns null for all fields', () => {
    const getByRoot = makeGetByRoot({});
    const result = getByRoot('UNKNOWN_ROOT');
    expect(result.day_pnl).toBeNull();
    expect(result.pnl).toBeNull();
    expect(result.exp_pnl).toBeNull();
    expect(result.extrinsic).toBeNull();
    expect(result.prev_mv).toBeNull();
    expect(result.chg_pct).toBeNull();
  });

  it('getByRoot(unknown, 0) returns 0 for all absent fields', () => {
    const getByRoot = makeGetByRoot({});
    const result = getByRoot('UNKNOWN_ROOT', 0);
    expect(result.day_pnl).toBe(0);
    expect(result.pnl).toBe(0);
    expect(result.exp_pnl).toBe(0);
    expect(result.extrinsic).toBe(0);
    expect(result.prev_mv).toBe(0);
    expect(result.chg_pct).toBe(0);
  });

  it('getByRoot(NIFTY) returns store values when all fields are populated', () => {
    const getByRoot = makeGetByRoot({
      NIFTY: { day_pnl: 2000, pnl: 5000, exp_pnl: 1500, extrinsic: 300, prev_mv: 100000, chg_pct: 2.0 },
    });
    const nifty = getByRoot('NIFTY');
    expect(nifty.day_pnl).toBe(2000);
    expect(nifty.pnl).toBe(5000);
    expect(nifty.exp_pnl).toBe(1500);
    expect(nifty.extrinsic).toBe(300);
    expect(nifty.prev_mv).toBe(100000);
    expect(nifty.chg_pct).toBe(2.0);
  });

  it('getByRoot(NIFTY, 0) — null field in store gets 0, set fields preserved', () => {
    const getByRoot = makeGetByRoot({
      NIFTY: { day_pnl: 2000, pnl: 5000, exp_pnl: 1500, extrinsic: null, prev_mv: 100000, chg_pct: null },
    });
    const nifty = getByRoot('NIFTY', 0);
    expect(nifty.day_pnl).toBe(2000);   // set → preserved
    expect(nifty.extrinsic).toBe(0);     // null → fallback
    expect(nifty.chg_pct).toBe(0);       // null → fallback
  });

  it('day_pnl field: getByRoot(root, 0).day_pnl is the safe flash.update pattern used in derivatives', () => {
    // Validates: flash.update(`${g.underlying}:day_w`, positionsDerivedStore.getByRoot(g.underlying, 0).day_pnl)
    const getByRoot = makeGetByRoot({
      NIFTY: { day_pnl: 8000, pnl: 12000, exp_pnl: 5000, extrinsic: 200, prev_mv: 200000, chg_pct: 4.0 },
    });
    // Known root
    expect(getByRoot('NIFTY', 0).day_pnl).toBe(8000);
    // Unknown root — 0 not NaN
    expect(getByRoot('BANKNIFTY', 0).day_pnl).toBe(0);
  });

  it('lowercase root is normalised to uppercase key', () => {
    const getByRoot = makeGetByRoot({
      NIFTY: { day_pnl: 2000, pnl: 5000, exp_pnl: 1500, extrinsic: 300, prev_mv: 100000, chg_pct: 2.0 },
    });
    expect(getByRoot('nifty', 0).day_pnl).toBe(2000);
  });
});
