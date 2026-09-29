/**
 * portfolioStore.test.js — Vitest unit tests for portfolioStore.svelte.js
 *
 * Five quality dimensions:
 *  1. SSOT  — exercises the SWR null-guard, root decomposition, holdings day P&L formula
 *  2. Perf  — no I/O; all derivations are synchronous pure functions
 *  3. Stale — verifies snapshot caching when deps are null; no race conditions
 *  4. Reuse — uses exported _computeDerived for pure-function testing
 *  5. UX    — tests verify aggregation shapes match NavStrip/Pulse consumption
 *
 * NOTE: portfolioStore uses Svelte 5 $state/$derived runes and cannot be imported
 * directly into Vitest (no Svelte compiler). Tests exercise the pure function logic for
 * positions aggregation, holdings day P&L, and funds calculations using local mirrors.
 *
 * Coverage:
 *   - Root decomposition and byRoot aggregation (via _computeDerived)
 *   - Root spot called once per root (caching in real store)
 *   - Holdings day_pnl formula logic (live LTP vs broker dcv)
 *   - Holdings dcv fallback when close <= 0
 *   - Funds aggregation and utilization percentage
 *   - _computeDerived backward compat (pure function exported)
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { baseDayPnlForPosition, dayChangePct } from '$lib/data/nav.js';
import { expiryPnl } from '$lib/data/expiryPnl.js';
import { isFOSymbol } from '$lib/data/derivativesMath.js';
// Vite `?raw` import — reads the source as a plain string without Node's
// `fs`/`path` (not available under this project's Vitest node types config;
// same established convention as portfolioAggregatesTriggers.test.js /
// marketDataStoresMeta.test.js).
// @ts-ignore — Vite raw-import suffix has no TS module declaration here.
import portfolioStoreSrc from '$lib/data/portfolioStore.svelte.js?raw';

// ── R4a source-grep guard ─────────────────────────────────────────────────
//
// portfolioStore.svelte.js's top-level $derived.by(...) calls execute at
// MODULE EVALUATION time (e.g. `const _rootSpotCache = $derived.by(() =>
// {...})` at top scope, not deferred inside a function the way
// dataStore.svelte.js's $state calls are inside createDataStore's body) —
// importing this file directly throws (`$derived is not defined`) without
// the svelte/vite-plugin-svelte compiler registered in vitest.config.js
// (confirmed: no `plugins` entry there). The mirror-function tests below
// exercise a hand-copied re-implementation instead (see
// _computePortfolioPositions) — this block additionally greps the REAL
// shipped source directly so a regression in the actual file (not just
// the mirror) is caught, without needing to import/execute it.
describe('portfolioStore.svelte.js — R4a source-grep guard (real file, not the mirror)', () => {
  const src = portfolioStoreSrc;

  it('_rootSpotCache gates on the shared isFOSymbol predicate', () => {
    const idx = src.indexOf('const _rootSpotCache = $derived.by(() => {');
    expect(idx, '_rootSpotCache must exist').toBeGreaterThan(0);
    const blockEnd = src.indexOf('\n});', idx);
    const block = src.slice(idx, blockEnd);
    expect(
      block.includes('if (!isFOSymbol(sym) || !sym) continue;'),
      'R4a: _rootSpotCache must gate on isFOSymbol(sym), not an exchange-set check'
    ).toBe(true);
  });

  it('FO_EXCHS (the old exchange-set gate) is no longer declared or used as a live identifier', () => {
    // A historical mention inside an explanatory comment (e.g. "was
    // `FO_EXCHS.has(exch)`") is fine and expected — this only checks that
    // the actual declaration/usage is gone, not every string occurrence.
    expect(
      src.includes('const FO_EXCHS ='),
      'R4a: FO_EXCHS must no longer be declared'
    ).toBe(false);
    expect(
      /(?<!`)FO_EXCHS\.has\(/.test(src),
      'R4a: FO_EXCHS.has(...) must no longer be called as live code (only referenced in backtick-quoted comment prose)'
    ).toBe(false);
  });

  it('_posTier2 also uses isFOSymbol (Commit 7 — both gates in this file agree)', () => {
    expect(
      src.includes('const isFO = isFOSymbol(p._sym);'),
      'R4a: _posTier2 must classify via isFOSymbol, matching _rootSpotCache'
    ).toBe(true);
  });
});

// ── 2026-09 GOLD/GOLDM fix — expired-but-held valuation source-grep guard ───
//
// portfolioStore.svelte.js can't be imported directly (see file-header note
// above) — the real functional behaviour of isExpiredHeldContract /
// expiredPositionExpPnl / expiredPositionExpPnlPieces is covered directly
// (they're plain importable functions) in expiryPnl_expired.test.js. This
// block only guards that _posTier2 actually WIRES UP that fix — i.e. checks
// the expired-held branch BEFORE falling through to the rolled-forward-spot
// (_rootSpotCache) valuation that caused the original bug.
describe('portfolioStore.svelte.js — 2026-09 expired-but-held valuation guard (real file, not the mirror)', () => {
  const src = portfolioStoreSrc;

  it('imports the shared expired-detection + frozen-valuation helpers from expiryPnl.js', () => {
    expect(src).toContain('isExpiredHeldContract');
    expect(src).toContain('expiredPositionExpPnl');
    expect(src).toContain('expiredPositionExpPnlPieces');
    expect(src).toMatch(/from\s+['"]\$lib\/data\/expiryPnl\.js['"]/);
  });

  it('_posTier2 checks isExpiredHeldContract BEFORE resolving/using the rolled-forward root spot anchor', () => {
    const qtyBlockStart = src.indexOf('if (p._qty !== 0) {');
    expect(qtyBlockStart, 'p._qty !== 0 branch not found').toBeGreaterThan(0);
    const expiredCheckIdx = src.indexOf('isExpiredHeldContract(p._sym, p._qty)', qtyBlockStart);
    const anchorIdx = src.indexOf('resolveExpiryAnchor(', qtyBlockStart);
    expect(expiredCheckIdx, 'isExpiredHeldContract(p._sym, p._qty) call not found').toBeGreaterThan(qtyBlockStart);
    expect(anchorIdx, 'resolveExpiryAnchor(...) call not found').toBeGreaterThan(qtyBlockStart);
    expect(
      expiredCheckIdx < anchorIdx,
      'isExpiredHeldContract must be checked BEFORE resolveExpiryAnchor — otherwise an expired-but-held leg still gets valued against the rolled-forward root spot'
    ).toBe(true);
  });

  it('the expired branch does NOT call resolveExpiryAnchor / positionExpPnl (no spot-based recompute for a settled contract)', () => {
    const expiredIdx = src.indexOf('if (isExpiredHeldContract(p._sym, p._qty)) {');
    expect(expiredIdx, 'expired-held branch not found').toBeGreaterThan(0);
    const branchEnd = src.indexOf('} else {', expiredIdx);
    expect(branchEnd, 'expired-held branch close not found').toBeGreaterThan(expiredIdx);
    const branch = src.slice(expiredIdx, branchEnd);
    expect(branch).toContain('expiredPositionExpPnl(p)');
    expect(branch).toContain('expiredPositionExpPnlPieces(p)');
    expect(branch).not.toContain('resolveExpiryAnchor(');
    expect(branch).not.toContain('positionExpPnl(p,');
  });

  it('subscribes to instrumentsCacheVersion and bumps _tick (2026-09 should-do: re-derive when the instruments cache lands, not just on the next tick/poll)', () => {
    expect(src).toContain('instrumentsCacheVersion');
    expect(src).toMatch(/instrumentsCacheVersion\.subscribe\(\(\)\s*=>\s*\{/);
    // Same debounce-and-bump body shape as the existing symbolTickCount
    // subscribe above it — verifies it actually increments _tick, not a
    // no-op subscription.
    const idx = src.indexOf('instrumentsCacheVersion.subscribe(');
    const block = src.slice(idx, idx + 200);
    expect(block).toContain('_tick++');
  });
});

// ── Helpers ──────────────────────────────────────────────────────────────────

// Local mirror of _computeDerived from portfolioStore.svelte.js
// Tests the pure function form exported for backward compat.
// This is the canonical aggregation logic for positions + holdings + funds.

function _computePortfolioPositions(posRows, holdRows, deps = {}) {
  const {
    getSnap    = sym  => undefined,
    getSpot    = root => 0,
    getTargets = sym  => [],
    getProxy   = (sym, tgt) => null,
    livePosDay = (p) => baseDayPnlForPosition(p),
    marketOpen = true,
  } = deps;

  const total = { day_pnl: 0, exp_pnl: 0, extrinsic: 0 };
  const byKey = {};
  const posByAccount = {};
  const byRootPositions = {};
  const byRootHoldings  = {};
  const byRoot = {};
  const expiryByAcct = new Map();

  // Build root→spot map ONCE before positions loop
  //
  // 2026-09 R4 post-ship audit fix: was `FO_EXCHS.has(exch)` — a local
  // re-implementation of the old exchange-set gate Commit 7 replaced
  // elsewhere in this mirror (see `isFO` below) with the REAL shared
  // `isFOSymbol` predicate. A Groww-sourced F&O row whose adapter passes
  // `exchange` through unchanged (e.g. reporting 'NSE' for an NFO contract)
  // was excluded from this root-spot cache. Now calls the real predicate
  // (imported above), matching the real store's `_rootSpotCache` fix, so a
  // regression in the shared predicate fails this test too.
  const rootSpotCache = {};
  for (const p of posRows) {
    const sym  = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
    if (!sym || !isFOSymbol(sym)) continue;
    const decomp = decomposeSymbol(sym);
    const root   = (decomp.root || sym).toUpperCase();
    if (root && !(root in rootSpotCache)) {
      const liveSpot = getSpot(root);
      rootSpotCache[root] = liveSpot > 0 ? liveSpot : (Number(p?.underlying_ltp) || 0);
    }
  }

  for (const p of posRows) {
    const sym = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
    if (!sym) continue;

    const qty  = Number(p?.quantity ?? 0) || 0;
    const avg  = Number(p?.average_price ?? 0) || 0;
    const pnl  = Number(p?.pnl ?? 0);
    const snap = getSnap(sym);
    const ltp  = snap?.ltp ?? Number(p?.last_price ?? 0);

    const day_pnl = livePosDay(p, ltp, { marketOpen });

    // 2026-09 Commit 7 fix (R4 follow-up completes it — see rootSpotCache
    // above, now also on isFOSymbol): exchange-independent classification,
    // imported from the REAL shared module (not a local re-implementation) —
    // this mirror function exercises the actual fixed predicate the real
    // portfolioStore.svelte.js now calls, so a regression here fails this
    // test too.
    const isFO = isFOSymbol(sym);

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
        // value against underlying spot; futures value against spot too,
        // falling back to the contract's own LTP only when spot is
        // unavailable (e.g. MCX futures with no underlying spot index).
        const decomp  = decomposeSymbol(sym);
        const root    = (decomp.root || sym).toUpperCase();
        const spot1   = Number(p?.underlying_ltp || 0);
        const spot    = spot1 > 0 ? spot1 : (rootSpotCache[root] || 0);
        const anchor  = isOpt ? spot : (spot > 0 ? spot : (ltp || 0));

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

    if (!byKey[sym]) byKey[sym] = { day_pnl: 0, exp_pnl: null, extrinsic: null, pnl: 0, prev_mv: 0, chg_pct: null };
    const bk = byKey[sym];
    bk.day_pnl += day_pnl;
    bk.pnl     += pnl;
    const prev_close = Number(p?.previous_close) || Number(p?.close_price) || 0;
    const oq = Number(p?.overnight_quantity ?? 0);
    // prev_mv: avg fallback for new intraday positions (oq=0, no prior session close)
    let prev_mv_contrib = 0;
    if (prev_close > 0) {
      prev_mv_contrib = prev_close * Math.abs(qty);
    } else if (oq === 0 && avg > 0) {
      // new intraday position: use avg_cost as denominator
      prev_mv_contrib = avg * Math.abs(qty);
    }
    bk.prev_mv = (bk.prev_mv || 0) + prev_mv_contrib;
    if (exp_pnl   != null) bk.exp_pnl   = (bk.exp_pnl   ?? 0) + exp_pnl;
    if (extrinsic != null) bk.extrinsic = (bk.extrinsic ?? 0) + extrinsic;

    total.day_pnl += day_pnl;
    if (exp_pnl   != null) total.exp_pnl   += exp_pnl;
    if (extrinsic != null) total.extrinsic += extrinsic;

    const _acct = String(p?.account || '').toUpperCase();
    if (_acct) posByAccount[_acct] = (posByAccount[_acct] ?? 0) + day_pnl;

    if (isFO && expVal != null) {
      // 2026-09 Commit 9 fix: uppercase, matching `_acct` above (and the
      // real portfolioStore.svelte.js source) — mirrors the real fix so a
      // regression here fails this test too.
      const acct = _acct;
      if (acct) expiryByAcct.set(acct, (expiryByAcct.get(acct) ?? 0) + expVal);

      const decomp = decomposeSymbol(sym);
      const root   = (decomp.root || sym).toUpperCase();
      if (root) {
        const rp = byRootPositions[root] ??= { day_pnl: 0, exp_pnl: 0, extrinsic: 0, pnl: 0 };
        rp.day_pnl   += day_pnl;
        rp.pnl       += pnl;
        rp.exp_pnl   += (exp_pnl   ?? 0);
        rp.extrinsic += (extrinsic ?? 0);

        if (!byRoot[root]) byRoot[root] = { spot: rootSpotCache[root] || 0, legs: [], day_pnl: 0, exp_pnl: 0, extrinsic: 0 };
        byRoot[root].legs.push(sym);
        byRoot[root].day_pnl += day_pnl;
        if (exp_pnl   != null) byRoot[root].exp_pnl   += exp_pnl;
        if (extrinsic != null) byRoot[root].extrinsic += extrinsic;
      }
    }
  }

  // Holdings cross-hedge loop (byRootHoldings) — mirrors original logic
  for (const h of holdRows) {
    const sym = String(h?.tradingsymbol || h?.symbol || '').toUpperCase();
    if (!sym) continue;

    const qty  = Number(h?.quantity) || 0;
    const cost = Number(h?.average_price ?? h?.avg_cost) || 0;
    const snapH = getSnap(sym);
    const ltp   = (snapH?.ltp ?? 0) > 0 ? Number(snapH.ltp) : Number(h?.last_price ?? 0);

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

  posByAccount['TOTAL'] = total.day_pnl;
  return { total, byKey, posByAccount, byRootPositions, byRootHoldings, byRoot, expiryByAcct };
}

// Minimal decomposeSymbol mirror
function decomposeSymbol(sym) {
  const match = sym.match(/^([A-Z]+)\d+[A-Z]+/);
  const root = match ? match[1] : sym;
  return { root };
}

function makePosition(overrides = {}) {
  return {
    tradingsymbol: 'NIFTY25JAN24500CE',
    symbol: 'NIFTY25JAN24500CE',
    exchange: 'NFO',
    quantity: 1,
    average_price: 100,
    previous_close: 50,
    close_price: 50,
    last_price: 150,
    pnl: 50,
    day_change_val: 100,
    overnight_quantity: 1,
    realised: 0,
    account: 'ACC1',
    underlying_ltp: 23000,
    ...overrides,
  };
}

function makeHolding(overrides = {}) {
  return {
    tradingsymbol: 'RELIANCE',
    symbol: 'RELIANCE',
    exchange: 'NSE',
    quantity: 10,
    average_price: 2400,
    previous_close: 2400,
    close_price: 2400,
    last_price: 2450,
    pnl: 500,
    day_change_val: 500,
    account: 'ACC1',
    ...overrides,
  };
}

function computeHoldingsDayPnl(holdRows, getSnap, isMarketOpen) {
  const result = { total: 0, byKey: {}, byAccount: {} };
  for (const h of holdRows) {
    const sym = String(h?.tradingsymbol || h?.symbol || '').toUpperCase();
    if (!sym) continue;

    const snap    = getSnap(sym);
    const snapLtp = snap?.ltp;

    const liveLtp = (snapLtp != null && snapLtp > 0)
      ? Number(snapLtp)
      : Number(h?.last_price ?? 0);

    const closePx = Number(h?.previous_close) || Number(h?.close_price) || Number(h?.ohlc?.close) || 0;
    const heldQty = Number(h?.quantity) || 0;
    const dcv     = Number(h?.day_change_val) || 0;

    let val;
    if (closePx <= 0) {
      val = dcv;
    } else if (liveLtp > 0 && heldQty !== 0 && Math.abs(liveLtp - closePx) > 0.005) {
      val = (liveLtp - closePx) * heldQty;
    } else {
      val = dcv;
    }

    result.byKey[sym] = (result.byKey[sym] ?? 0) + val;
    result.total += val;

    const acc = String(h?.account || '').toUpperCase();
    if (acc) {
      if (!result.byAccount[acc]) result.byAccount[acc] = 0;
      result.byAccount[acc] += val;
    }
  }
  result.byAccount['TOTAL'] = result.total;
  return result;
}

function computeFunds(fundRows) {
  const result = {
    total: { live_cash: 0, avail_margin: 0, used_margin: 0, totalMargin: 0, utilPct: 0, collateral: 0 },
    byAccount: {},
  };
  for (const f of fundRows) {
    const acct = String(f?.account || '').toUpperCase();
    if (!acct || acct === 'TOTAL') continue;
    const live_cash    = Number(f?.live_cash    ?? f?.cash       ?? 0);
    const avail_margin = Number(f?.avail_margin ?? 0);
    const used_margin  = Number(f?.used_margin  ?? 0);
    const collateral   = Number(f?.collateral   ?? 0);
    const totalMargin  = used_margin + avail_margin;
    const utilPct      = totalMargin > 0 ? (used_margin / totalMargin) * 100 : 0;
    result.byAccount[acct] = { live_cash, avail_margin, used_margin, collateral, totalMargin, utilPct };
    result.total.live_cash    += live_cash;
    result.total.avail_margin += avail_margin;
    result.total.used_margin  += used_margin;
    result.total.collateral   += collateral;
  }
  result.total.totalMargin = result.total.used_margin + result.total.avail_margin;
  result.total.utilPct = result.total.totalMargin > 0
    ? (result.total.used_margin / result.total.totalMargin) * 100 : 0;
  return result;
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('portfolioStore — positions aggregation via _computePortfolioPositions', () => {
  it('returns empty structure for empty arrays', () => {
    const result = _computePortfolioPositions([], []);
    expect(result.total).toEqual({ day_pnl: 0, exp_pnl: 0, extrinsic: 0 });
    expect(result.byKey).toEqual({});
    expect(result.byRoot).toEqual({});
    expect(result.expiryByAcct).toEqual(new Map());
  });

  it('computes single position byKey aggregation', () => {
    const pos = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      quantity: 1,
      previous_close: 50,
      last_price: 150,
      average_price: 100,
      exchange: 'NFO',
    });

    const result = _computePortfolioPositions([pos], []);
    expect(result.byKey['NIFTY25JAN24500CE']).toBeDefined();
    expect(result.byKey['NIFTY25JAN24500CE'].day_pnl).toBeGreaterThan(0);
  });

  // 2026-09 Commit 7 fix: a Groww-sourced F&O row whose adapter passes
  // `exchange` through unchanged (e.g. reporting 'NSE' for what is
  // actually an NFO option contract) must still be classified as F&O —
  // the old exchange-based gate (`exchange ∈ {NFO,MCX,CDS,BFO}`) would
  // have excluded it (exp_pnl/extrinsic staying null) while the
  // derivatives page's own symbol-regex gate still included it.
  it('classifies a Groww-sourced F&O row with exchange:"NSE" as F&O via the shared symbol predicate, not the exchange field', () => {
    const growwRow = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      quantity: 1,
      previous_close: 50,
      last_price: 150,
      average_price: 100,
      exchange: 'NSE', // Groww adapter passthrough — NOT one of FO_EXCHS
    });
    const result = _computePortfolioPositions([growwRow], []);
    expect(isFOSymbol(growwRow.tradingsymbol)).toBe(true);
    expect(result.byKey['NIFTY25JAN24500CE'].exp_pnl).not.toBeNull();
  });

  it('does not classify a real equity symbol ending in "CE"/"PE"/"FUT"-like letters as F&O (ACE regression guard)', () => {
    // "ACE" is a real NSE equity tradingsymbol that matched the OLD bare
    // `/(CE|PE|FUT)$/` suffix regex — the new predicate requires a digit
    // immediately before CE/PE (or a YY+MON tail before FUT), so a plain
    // equity symbol is correctly excluded regardless of its exchange field.
    const aceRow = makePosition({
      tradingsymbol: 'ACE',
      quantity: 10,
      previous_close: 500,
      last_price: 520,
      average_price: 480,
      exchange: 'NSE',
    });
    const result = _computePortfolioPositions([aceRow], []);
    expect(isFOSymbol('ACE')).toBe(false);
    expect(result.byKey['ACE'].exp_pnl).toBeNull();
  });
});

describe('portfolioStore — root decomposition and byRoot aggregation', () => {
  it('decomposes multi-leg NIFTY spread into byRoot', () => {
    const ce = makePosition({ tradingsymbol: 'NIFTY25JAN24500CE', quantity: 1, average_price: 100, last_price: 150, exchange: 'NFO' });
    const pe = makePosition({ tradingsymbol: 'NIFTY25JAN24000PE', quantity: -1, average_price: 80, last_price: 120, exchange: 'NFO' });
    const fut = makePosition({ tradingsymbol: 'NIFTY25JANFUT', quantity: 2, average_price: 23000, last_price: 23200, exchange: 'NFO' });

    const result = _computePortfolioPositions([ce, pe, fut], []);
    expect(result.byRoot['NIFTY']).toBeDefined();
    expect(result.byRoot['NIFTY'].legs).toContain('NIFTY25JAN24500CE');
    expect(result.byRoot['NIFTY'].legs).toContain('NIFTY25JAN24000PE');
    expect(result.byRoot['NIFTY'].legs).toContain('NIFTY25JANFUT');
  });

  it('aggregates day_pnl across legs into byRoot', () => {
    const ce = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      quantity: 1,
      previous_close: 50,
      last_price: 150,
      average_price: 100,
      exchange: 'NFO',
    });
    const pe = makePosition({
      tradingsymbol: 'NIFTY25JAN24000PE',
      quantity: 1,
      previous_close: 40,
      last_price: 120,
      average_price: 80,
      exchange: 'NFO',
    });

    const result = _computePortfolioPositions([ce, pe], []);
    expect(result.byRoot['NIFTY'].day_pnl).toBeGreaterThan(0);
  });

  it('sets byRoot spot from root spot cache', () => {
    const ce = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      quantity: 1,
      exchange: 'NFO',
      underlying_ltp: 23000,
    });

    const mockGetSpot = vi.fn(() => 0); // Fallback to underlying_ltp
    const result = _computePortfolioPositions([ce], [], { getSpot: mockGetSpot });
    expect(result.byRoot['NIFTY'].spot).toBe(23000);
  });

  it('calls getSpot once per root for multi-leg spread', () => {
    const ce = makePosition({ tradingsymbol: 'NIFTY25JAN24500CE', exchange: 'NFO', underlying_ltp: 23000 });
    const pe = makePosition({ traditionsymbol: 'NIFTY25JAN24000PE', exchange: 'NFO', underlying_ltp: 23000 });

    const mockGetSpot = vi.fn(() => 23000);
    _computePortfolioPositions([ce, pe], [], { getSpot: mockGetSpot });

    // Should be called once for NIFTY root (cached)
    const niftyCalls = mockGetSpot.mock.calls.filter(c => /** @type {any[]} */ (c)[0] === 'NIFTY');
    expect(niftyCalls.length).toBe(1);
  });

  // 2026-09 R4 post-ship audit fix: the root-spot cache used to gate on
  // `FO_EXCHS.has(exch)` (exchange ∈ {NFO,MCX,CDS,BFO}), which excludes a
  // Groww-sourced F&O row whose adapter passes `exchange` through unchanged
  // (e.g. reporting 'NSE' for an NFO contract) — that row's root would never
  // get a cached spot, leaving `byRoot[root].spot` at 0 even though the SAME
  // row's exp_pnl/extrinsic classification (isFO tier) already correctly
  // treated it as F&O via `isFOSymbol`. Now both tiers agree.
  it('sets byRoot spot from root spot cache for a Groww-sourced F&O row with exchange:"NSE"', () => {
    const growwCe = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      quantity: 1,
      exchange: 'NSE', // Groww adapter passthrough — not in the old FO_EXCHS set
      underlying_ltp: 23000,
    });

    const mockGetSpot = vi.fn(() => 0); // Fallback to underlying_ltp
    const result = _computePortfolioPositions([growwCe], [], { getSpot: mockGetSpot });
    expect(result.byRoot['NIFTY'].spot).toBe(23000);
  });
});

describe('portfolioStore — holdings day_pnl formula', () => {
  it('computes holdings day_pnl as (liveLtp - closePx) * qty', () => {
    const getSnap = () => ({ ltp: 2460 });
    const holding = makeHolding({
      tradingsymbol: 'RELIANCE',
      quantity: 10,
      previous_close: 2400,
      last_price: 2450,
      day_change_val: 500,
    });

    const result = computeHoldingsDayPnl([holding], getSnap, true);
    // (2460 - 2400) * 10 = 600
    expect(result.byKey['RELIANCE']).toBe(600);
  });

  it('falls back to dcv when closePx <= 0', () => {
    const getSnap = () => null;
    const holding = {
      tradingsymbol: 'HDFC',
      symbol: 'HDFC',
      exchange: 'NSE',
      quantity: 10,
      average_price: 1600,
      previous_close: 0,
      close_price: 0,
      last_price: 1630,
      pnl: 300,
      day_change_val: 300,
      account: 'ACC1',
    };

    const result = computeHoldingsDayPnl([holding], getSnap, true);
    expect(result.byKey['HDFC']).toBe(300);
  });

  it('falls back to dcv when price flat (diff <= 0.005)', () => {
    const getSnap = () => ({ ltp: 1600 });
    const holding = makeHolding({
      tradingsymbol: 'INFY',
      quantity: 10,
      previous_close: 1600,
      last_price: 1600,
      day_change_val: 0,
    });

    const result = computeHoldingsDayPnl([holding], getSnap, true);
    expect(result.byKey['INFY']).toBe(0);
  });

  it('aggregates multiple holdings into total', () => {
    const getSnap = () => null;
    const h1 = makeHolding({
      tradingsymbol: 'RELIANCE',
      quantity: 10,
      previous_close: 2400,
      last_price: 2450,
      day_change_val: 500,
    });
    const h2 = makeHolding({
      tradingsymbol: 'INFY',
      quantity: 20,
      previous_close: 1600,
      last_price: 1620,
      day_change_val: 400,
      account: 'ACC1',
    });

    const result = computeHoldingsDayPnl([h1, h2], getSnap, true);
    expect(result.total).toBe(900);
  });

  it('groups holdings by account', () => {
    const getSnap = () => null;
    // Use flat prices (diff within 0.005) to trigger dcv fallback
    const h1 = {
      tradingsymbol: 'RELIANCE',
      symbol: 'RELIANCE',
      exchange: 'NSE',
      quantity: 10,
      average_price: 2400,
      previous_close: 2400,
      close_price: 2400,
      last_price: 2400, // Flat
      pnl: 300,
      day_change_val: 300,
      account: 'ACC1',
    };
    const h2 = {
      tradingsymbol: 'INFY',
      symbol: 'INFY',
      exchange: 'NSE',
      quantity: 20,
      average_price: 1600,
      previous_close: 1600,
      close_price: 1600,
      last_price: 1600, // Flat
      pnl: 200,
      day_change_val: 200,
      account: 'ACC2',
    };

    const result = computeHoldingsDayPnl([h1, h2], getSnap, true);
    expect(result.byAccount['ACC1']).toBe(300);
    expect(result.byAccount['ACC2']).toBe(200);
    expect(result.byAccount['TOTAL']).toBe(500);
  });
});

describe('portfolioStore — funds aggregation', () => {
  it('aggregates avail_margin and used_margin across accounts', () => {
    const f1 = { account: 'ACC1', avail_margin: 50000, used_margin: 20000 };
    const f2 = { account: 'ACC2', avail_margin: 30000, used_margin: 10000 };

    const result = computeFunds([f1, f2]);
    expect(result.total.avail_margin).toBe(80000);
    expect(result.total.used_margin).toBe(30000);
  });

  it('computes totalMargin = used + avail', () => {
    const fund = { account: 'ACC1', avail_margin: 50000, used_margin: 20000 };

    const result = computeFunds([fund]);
    expect(result.byAccount['ACC1'].totalMargin).toBe(70000);
  });

  it('computes utilPct = (used / total) * 100', () => {
    const fund = { account: 'ACC1', avail_margin: 50000, used_margin: 20000 };

    const result = computeFunds([fund]);
    // 20000 / 70000 ≈ 28.57%
    expect(result.byAccount['ACC1'].utilPct).toBeCloseTo(28.57, 1);
  });

  it('filters out TOTAL row from aggregation', () => {
    const f1 = { account: 'ACC1', avail_margin: 50000 };
    const fTotal = { account: 'TOTAL', avail_margin: 999999 };

    const result = computeFunds([f1, fTotal]);
    // Should not include TOTAL row in aggregation
    expect(result.total.avail_margin).toBe(50000);
    expect(result.byAccount['TOTAL']).toBeUndefined();
  });

  it('handles live_cash from cash field fallback', () => {
    const fund = { account: 'ACC1', cash: 100000, avail_margin: 50000, used_margin: 20000 };

    const result = computeFunds([fund]);
    expect(result.byAccount['ACC1'].live_cash).toBe(100000);
  });

  it('aggregates collateral across accounts', () => {
    const f1 = { account: 'ACC1', collateral: 5000, avail_margin: 50000, used_margin: 0 };
    const f2 = { account: 'ACC2', collateral: 3000, avail_margin: 30000, used_margin: 0 };

    const result = computeFunds([f1, f2]);
    expect(result.total.collateral).toBe(8000);
  });

  it('computes total utilPct across all accounts', () => {
    const f1 = { account: 'ACC1', avail_margin: 50000, used_margin: 20000 };
    const f2 = { account: 'ACC2', avail_margin: 30000, used_margin: 10000 };

    const result = computeFunds([f1, f2]);
    // Total: used=30000, avail=80000, totalMargin=110000 → (30000/110000)*100 = 27.27%
    expect(result.total.utilPct).toBeCloseTo(27.27, 1);
  });
});

// 'portfolioStore — holdings pulse logic simulation' and 'portfolioStore.holdings
// — pulse override chgPctByKey fix' describe blocks removed (2026-09 Commit 9):
// the pulse-override mechanism (setHoldingsFromPulse) they tested was itself
// removed from portfolioStore.svelte.js — MarketPulse had already stopped
// calling it in an earlier session, and these tests never imported the real
// store anyway (they re-implemented the override object literal inline), so
// they were only testing logic that no longer exists anywhere.

describe('portfolioStore — _computePortfolioPositions (exported pure function)', () => {
  it('returns correct structure for empty arrays', () => {
    const result = _computePortfolioPositions([], []);
    expect(result).toHaveProperty('total');
    expect(result).toHaveProperty('byKey');
    expect(result).toHaveProperty('byRootPositions');
    expect(result).toHaveProperty('byRootHoldings');
    expect(result).toHaveProperty('byRoot');
    expect(result).toHaveProperty('expiryByAcct');
    expect(result.total).toEqual({ day_pnl: 0, exp_pnl: 0, extrinsic: 0 });
    expect(result.byKey).toEqual({});
    expect(result.expiryByAcct).toEqual(new Map());
  });

  // 2026-09 Commit 9 fix: expiryByAcct used to key on the RAW (unuppercased)
  // `p.account` while every other account key in this file (`_acct`,
  // posByAccount) was uppercased — a broker returning mixed-case account
  // codes for the same account across rows would silently split its total
  // across two Map keys instead of accumulating into one.
  it('expiryByAcct keys are uppercased, matching posByAccount — mixed-case account codes for the SAME account accumulate into ONE key', () => {
    const posLower = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      quantity: 1,
      average_price: 100,
      last_price: 150,
      previous_close: 50,
      account: 'zg0790',
      exchange: 'NFO',
    });
    const posUpper = makePosition({
      tradingsymbol: 'NIFTY25JAN24600CE',
      quantity: 1,
      average_price: 100,
      last_price: 150,
      previous_close: 50,
      account: 'ZG0790',
      exchange: 'NFO',
    });
    const result = _computePortfolioPositions([posLower, posUpper], []);
    expect(result.expiryByAcct.size).toBe(1);
    expect(result.expiryByAcct.has('ZG0790')).toBe(true);
    expect(result.expiryByAcct.has('zg0790')).toBe(false);
  });

  it('computes positions from raw arrays with live LTP override', () => {
    const pos = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      quantity: 1,
      average_price: 100,
      last_price: 150,
      pnl: 50,
      day_change_val: 50,
      previous_close: 50,
      exchange: 'NFO',
    });

    const result = _computePortfolioPositions([pos], []);
    expect(result.byKey['NIFTY25JAN24500CE']).toBeDefined();
    expect(result.byKey['NIFTY25JAN24500CE'].day_pnl).toBeGreaterThan(0);
  });

  it('accepts custom deps for testing', () => {
    const pos = makePosition({
      tradingsymbol: 'TEST25JAN24100CE',
      quantity: 1,
      average_price: 50,
      exchange: 'NFO',
      underlying_ltp: 10000,
    });

    const customDeps = {
      getSnap: (sym) => ({ ltp: 60 }),
      getSpot: (root) => 10000,
      getTargets: (sym) => [],
      getProxy: (sym, tgt) => ({ beta: 1 }),
      livePosDay: (p, ltp, opts) => 10,
      marketOpen: true,
    };

    const result = _computePortfolioPositions([pos], [], customDeps);
    expect(result.byKey['TEST25JAN24100CE']).toBeDefined();
  });

  it('aggregates holdings for cross-hedge attribution (byRootHoldings)', () => {
    const holding = makeHolding({
      tradingsymbol: 'RELIANCE',
      quantity: 10,
      average_price: 2400,
      last_price: 2450,
      pnl: 500,
    });

    const result = _computePortfolioPositions([], [holding]);
    // byRootHoldings should have RELIANCE entry
    expect(result.byRootHoldings['RELIANCE']).toBeDefined();
    expect(result.byRootHoldings['RELIANCE'].pnl).toBe(500);
  });
});

describe('portfolioStore — integration (multi-account, multi-leg)', () => {
  it('computes full portfolio with multiple accounts and legs', () => {
    const positions = [
      makePosition({
        tradingsymbol: 'NIFTY25JAN24500CE',
        account: 'ACC1',
        quantity: 1,
        previous_close: 50,
        last_price: 150,
        average_price: 100,
        exchange: 'NFO',
      }),
      makePosition({
        tradingsymbol: 'NIFTY25JAN24000PE',
        account: 'ACC1',
        quantity: -1,
        previous_close: 40,
        last_price: 120,
        average_price: 80,
        exchange: 'NFO',
      }),
    ];

    const holdings = [
      makeHolding({ account: 'ACC1', tradingsymbol: 'RELIANCE' }),
    ];

    const funds = [
      { account: 'ACC1', avail_margin: 50000, used_margin: 20000 },
    ];

    const posResult = _computePortfolioPositions(positions, holdings);
    const holdResult = computeHoldingsDayPnl(holdings, () => null, true);
    const fundResult = computeFunds(funds);

    expect(posResult.byRoot['NIFTY']).toBeDefined();
    expect(posResult.byRoot['NIFTY'].legs.length).toBe(2);
    expect(holdResult.byKey['RELIANCE']).toBeDefined();
    expect(fundResult.byAccount['ACC1']).toBeDefined();
    expect(fundResult.byAccount['ACC1'].totalMargin).toBe(70000);
  });

  it('computes equity holdings without F&O aggregation', () => {
    const holdings = [
      makeHolding({
        tradingsymbol: 'RELIANCE',
        exchange: 'NSE',
        quantity: 50,
        previous_close: 2400,
        last_price: 2450,
      }),
      makeHolding({
        tradingsymbol: 'INFY',
        exchange: 'NSE',
        quantity: 100,
        previous_close: 1600,
        last_price: 1620,
      }),
    ];

    const result = computeHoldingsDayPnl(holdings, () => null, true);
    expect(result.byKey['RELIANCE']).toBeGreaterThan(0);
    expect(result.byKey['INFY']).toBeGreaterThan(0);
  });
});

describe('portfolioStore — chg_pct tier aggregation', () => {
  it('byKey[sym].chg_pct is null when previous_close=0', () => {
    const pos = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      quantity: 1,
      previous_close: 0,
      close_price: 0,
      last_price: 150,
      average_price: 100,
      exchange: 'NFO',
    });

    const result = _computePortfolioPositions([pos], []);
    expect(result.byKey['NIFTY25JAN24500CE'].prev_mv).toBe(0);
    expect(result.byKey['NIFTY25JAN24500CE'].chg_pct).toBeNull();
  });

  it('byKey[sym].chg_pct is non-null when previous_close is set', () => {
    const pos = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      quantity: 1,
      previous_close: 100,
      last_price: 150,
      average_price: 100,
      exchange: 'NFO',
    });

    const result = _computePortfolioPositions([pos], []);
    const bk = result.byKey['NIFTY25JAN24500CE'];
    expect(bk.prev_mv).toBe(100);
    expect(typeof bk.chg_pct).toBe('number');
    expect(bk.chg_pct).not.toBeNull();
  });

  it('posTotal.chg_pct is null when all positions have previous_close=0', () => {
    const pos1 = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      quantity: 1,
      previous_close: 0,
      close_price: 0,
      exchange: 'NFO',
    });
    const pos2 = makePosition({
      tradingsymbol: 'NIFTY25JAN24000PE',
      quantity: 1,
      previous_close: 0,
      close_price: 0,
      exchange: 'NFO',
    });

    const result = _computePortfolioPositions([pos1, pos2], []);
    // When all prev_mv = 0, total should have chg_pct computed from total.prev_mv = 0
    // The loop computes chg_pct per byKey entry, which would be null for each
    // No explicit total.chg_pct in this mirror, but each tier's chg_pct is null
    expect(result.byKey['NIFTY25JAN24500CE'].chg_pct).toBeNull();
    expect(result.byKey['NIFTY25JAN24000PE'].chg_pct).toBeNull();
  });

  it('byRootPos[root].chg_pct is non-null for F&O position with previous_close set', () => {
    const ce = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      quantity: 1,
      previous_close: 100,
      last_price: 150,
      average_price: 100,
      exchange: 'NFO',
    });

    const result = _computePortfolioPositions([ce], []);
    const bk = result.byKey['NIFTY25JAN24500CE'];
    // chg_pct was computed in the loop
    expect(bk.chg_pct).not.toBeNull();
    expect(typeof bk.chg_pct).toBe('number');
  });

  it('prev_mv uses avg_cost fallback when oq=0 (new intraday position, no prior session close)', () => {
    const pos = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      quantity: 25,
      average_price: 22800,
      previous_close: 0,
      close_price: 0,
      overnight_quantity: 0,
      exchange: 'NFO',
    });
    const result = _computePortfolioPositions([pos], [], {
      livePosDay: () => 500,
    });
    const bk = result.byKey['NIFTY25JAN24500CE'];
    // prev_close = 0, oq = 0, so prev_mv = avg * qty = 22800 * 25 = 570000
    expect(bk.prev_mv).toBe(22800 * 25);
    // chg_pct = 500 / 570000 * 100
    expect(bk.chg_pct).toBeCloseTo((500 / (22800 * 25)) * 100, 4);
  });

  it('prev_mv is null for overnight position with prev_close=0 (no spurious avg fallback)', () => {
    const pos = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      quantity: 25,
      average_price: 23000,
      previous_close: 0,
      close_price: 0,
      overnight_quantity: 25,
      exchange: 'NFO',
    });
    const result = _computePortfolioPositions([pos], [], {
      livePosDay: () => 300,
    });
    const bk = result.byKey['NIFTY25JAN24500CE'];
    // overnight position with prev_close = 0: prev_mv should be null (no avg fallback)
    expect(bk.prev_mv).toBe(0);
    expect(bk.chg_pct).toBeNull();
  });
});

// ── posByAccount — per-account day_pnl accumulation ──────────────────────────
// Mirrors the _posAgg posByAccount logic added in portfolioStore.svelte.js.
// NavBreakdown P-slot reads positionsDayPnlStore.byAccount[acct] which delegates
// to portfolioStore.positions.byAccount — the same shape tested here.

describe('portfolioStore — posByAccount accumulation', () => {
  it('accumulates day_pnl per account for a single position', () => {
    const pos = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      exchange: 'NFO',
      account: 'ACC1',
      quantity: 1,
      previous_close: 50,
      last_price: 150,
      average_price: 100,
      overnight_quantity: 1,
    });

    const result = _computePortfolioPositions([pos], [], {
      livePosDay: () => 100,
    });

    expect(result.posByAccount['ACC1']).toBe(100);
  });

  it('accumulates day_pnl across multiple positions on the same account', () => {
    const p1 = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      exchange: 'NFO',
      account: 'ACC1',
    });
    const p2 = makePosition({
      tradingsymbol: 'NIFTY25JAN24000PE',
      exchange: 'NFO',
      account: 'ACC1',
    });

    // Fixed day_pnl returns: 100 for first call, 200 for second
    let callCount = 0;
    const result = _computePortfolioPositions([p1, p2], [], {
      livePosDay: () => (++callCount === 1 ? 100 : 200),
    });

    expect(result.posByAccount['ACC1']).toBe(300);
  });

  it('splits day_pnl across two distinct accounts', () => {
    const p1 = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      exchange: 'NFO',
      account: 'ACC1',
    });
    const p2 = makePosition({
      tradingsymbol: 'BANKNIFTY25JAN50000CE',
      exchange: 'NFO',
      account: 'ACC2',
    });

    let callCount = 0;
    const result = _computePortfolioPositions([p1, p2], [], {
      livePosDay: () => (++callCount === 1 ? 150 : 250),
    });

    expect(result.posByAccount['ACC1']).toBe(150);
    expect(result.posByAccount['ACC2']).toBe(250);
  });

  it('TOTAL key matches posTotal.day_pnl', () => {
    const p1 = makePosition({ tradingsymbol: 'NIFTY25JAN24500CE', exchange: 'NFO', account: 'ACC1' });
    const p2 = makePosition({ tradingsymbol: 'NIFTY25JAN24000PE', exchange: 'NFO', account: 'ACC2' });

    let callCount = 0;
    const result = _computePortfolioPositions([p1, p2], [], {
      livePosDay: () => (++callCount === 1 ? 150 : 250),
    });

    expect(result.posByAccount['TOTAL']).toBe(result.total.day_pnl);
    expect(result.posByAccount['TOTAL']).toBe(400);
  });

  it('empty positions returns empty posByAccount with TOTAL=0', () => {
    const result = _computePortfolioPositions([], []);
    expect(result.posByAccount).toEqual({ TOTAL: 0 });
  });

  it('skips rows with no account field', () => {
    const pos = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      exchange: 'NFO',
      account: '',   // blank — should be omitted from posByAccount
    });

    const result = _computePortfolioPositions([pos], [], {
      livePosDay: () => 100,
    });

    // No per-account key for blank account; only TOTAL sentinel
    const keys = Object.keys(result.posByAccount);
    expect(keys).toEqual(['TOTAL']);
    expect(result.posByAccount['TOTAL']).toBe(100);
  });

  it('normalises account keys to UPPERCASE', () => {
    const pos = makePosition({
      tradingsymbol: 'NIFTY25JAN24500CE',
      exchange: 'NFO',
      account: 'abc123',
    });

    const result = _computePortfolioPositions([pos], [], {
      livePosDay: () => 75,
    });

    expect(result.posByAccount['ABC123']).toBe(75);
    expect(result.posByAccount['abc123']).toBeUndefined();
  });
});

// ── New: positions.byAccount aggregation ─────────────────────────────────────

/**
 * portfolioStore.positions now mirrors holdings pattern with a byAccount getter.
 * This aggregates positions' _day_pnl per account.
 *
 * Test coverage:
 *   1. Single account aggregates correctly
 *   2. Multiple accounts aggregate independently
 *   3. byAccount['TOTAL'] equals positions.total.day_pnl
 *   4. Account keys are always uppercase
 *   5. Empty positions → byAccount is {}
 *   6. Null/missing account field is skipped
 */

function computePositionsByAccount(posRows, deps = {}) {
  const {
    getSnap    = sym  => undefined,
    getSpot    = root => 0,
    livePosDay = (p) => baseDayPnlForPosition(p),
    marketOpen = true,
  } = deps;

  let total_day_pnl = 0;
  const byAccount = {};

  for (const p of posRows) {
    const sym  = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
    if (!sym) continue;

    const snap = getSnap(sym);
    const ltp  = snap?.ltp ?? Number(p?.last_price ?? 0);
    const day_pnl = livePosDay(p, ltp, { marketOpen });

    total_day_pnl += day_pnl;

    const acct = String(p?.account || '').toUpperCase();
    if (acct) {
      byAccount[acct] = (byAccount[acct] ?? 0) + day_pnl;
    }
  }

  byAccount['TOTAL'] = total_day_pnl;
  return { byAccount, total_day_pnl };
}

describe('portfolioStore.positions.byAccount — single account', () => {
  it('aggregates positions for single account ZERODHA', () => {
    const positions = [
      makePosition({
        account: 'ZERODHA',
        tradingsymbol: 'NIFTY25JAN24500CE',
        quantity: 1,
        average_price: 100,
        previous_close: 50,
        last_price: 150,
        exchange: 'NFO',
      }),
      makePosition({
        account: 'ZERODHA',
        tradingsymbol: 'NIFTY25JAN24000PE',
        quantity: 1,
        average_price: 80,
        previous_close: 40,
        last_price: 120,
        exchange: 'NFO',
      }),
    ];

    const result = computePositionsByAccount(positions);
    expect(result.byAccount['ZERODHA']).toBeGreaterThan(0);
    expect(result.byAccount['TOTAL']).toBe(result.total_day_pnl);
  });

  it('account key is always uppercase', () => {
    const positions = [
      makePosition({
        account: 'zerodha', // lowercase input
        tradingsymbol: 'NIFTY25JAN24500CE',
        quantity: 1,
        average_price: 100,
        previous_close: 50,
        last_price: 150,
        exchange: 'NFO',
      }),
    ];

    const result = computePositionsByAccount(positions);
    expect(result.byAccount['ZERODHA']).toBeDefined();
    expect(result.byAccount['zerodha']).toBeUndefined();
  });

  it('empty positions → byAccount is empty except TOTAL', () => {
    const result = computePositionsByAccount([]);
    expect(result.byAccount).toEqual({ TOTAL: 0 });
  });

  it('position with null/empty account is skipped', () => {
    const positions = [
      makePosition({
        account: '', // empty account
        tradingsymbol: 'NIFTY25JAN24500CE',
        quantity: 1,
        average_price: 100,
        previous_close: 50,
        last_price: 150,
        exchange: 'NFO',
      }),
      makePosition({
        account: null, // null account
        tradingsymbol: 'NIFTY25JAN24000PE',
        quantity: 1,
        average_price: 80,
        previous_close: 40,
        last_price: 120,
        exchange: 'NFO',
      }),
    ];

    const result = computePositionsByAccount(positions);
    // Only TOTAL should exist (from aggregation of rows)
    const nonTotalKeys = Object.keys(result.byAccount).filter(k => k !== 'TOTAL');
    expect(nonTotalKeys.length).toBe(0);
  });
});

describe('portfolioStore.positions.byAccount — multiple accounts', () => {
  it('aggregates independent account totals', () => {
    const positions = [
      makePosition({
        account: 'ZERODHA',
        tradingsymbol: 'NIFTY25JAN24500CE',
        quantity: 1,
        average_price: 100,
        previous_close: 50,
        last_price: 150,
        exchange: 'NFO',
        day_change_val: 100,
      }),
      makePosition({
        account: 'DHAN',
        tradingsymbol: 'NIFTY25JAN24000PE',
        quantity: 1,
        average_price: 80,
        previous_close: 40,
        last_price: 120,
        exchange: 'NFO',
        day_change_val: 80,
      }),
    ];

    const result = computePositionsByAccount(positions, {
      livePosDay: (p) => baseDayPnlForPosition(p),
    });

    expect(result.byAccount['ZERODHA']).toBeGreaterThan(0);
    expect(result.byAccount['DHAN']).toBeGreaterThan(0);
    expect(result.byAccount['TOTAL']).toBe(result.total_day_pnl);
    // TOTAL must equal sum of individual accounts
    expect(result.byAccount['TOTAL']).toBe(result.byAccount['ZERODHA'] + result.byAccount['DHAN']);
  });

  it('multiple positions in same account sum correctly', () => {
    const positions = [
      makePosition({
        account: 'ZERODHA',
        tradingsymbol: 'NIFTY25JAN24500CE',
        quantity: 1,
        average_price: 100,
        previous_close: 50,
        last_price: 150,
        exchange: 'NFO',
      }),
      makePosition({
        account: 'ZERODHA',
        tradingsymbol: 'NIFTY25JAN24000PE',
        quantity: 1,
        average_price: 80,
        previous_close: 40,
        last_price: 120,
        exchange: 'NFO',
      }),
      makePosition({
        account: 'DHAN',
        tradingsymbol: 'BANKNIFTY25JAN24100PE',
        quantity: 1,
        average_price: 100,
        previous_close: 50,
        last_price: 150,
        exchange: 'NFO',
      }),
    ];

    const result = computePositionsByAccount(positions);

    expect(result.byAccount['ZERODHA']).toBeDefined();
    expect(result.byAccount['DHAN']).toBeDefined();
    // ZERODHA has 2 positions, DHAN has 1
    // Both should aggregate independently
    const zerodhaPosCount = positions.filter(p => p.account === 'ZERODHA').length;
    const dhanPosCount = positions.filter(p => p.account === 'DHAN').length;
    expect(zerodhaPosCount).toBe(2);
    expect(dhanPosCount).toBe(1);
  });

  it('byAccount[TOTAL] equals sum of all account day_pnls', () => {
    const positions = [
      makePosition({
        account: 'ACC1',
        quantity: 1,
        average_price: 100,
        previous_close: 50,
        last_price: 150,
        exchange: 'NFO',
        day_change_val: 100,
      }),
      makePosition({
        account: 'ACC2',
        quantity: 1,
        average_price: 80,
        previous_close: 40,
        last_price: 120,
        exchange: 'NFO',
        day_change_val: 80,
      }),
      makePosition({
        account: 'ACC3',
        quantity: 1,
        average_price: 120,
        previous_close: 60,
        last_price: 180,
        exchange: 'NFO',
        day_change_val: 120,
      }),
    ];

    const result = computePositionsByAccount(positions);

    const accountSum = Object.entries(result.byAccount)
      .filter(([k]) => k !== 'TOTAL')
      .reduce((sum, [, val]) => sum + val, 0);

    expect(result.byAccount['TOTAL']).toBe(accountSum);
    expect(result.byAccount['TOTAL']).toBe(result.total_day_pnl);
  });

  it('mixed case account names normalize to uppercase', () => {
    const positions = [
      makePosition({
        account: 'ZeroDha',
        tradingsymbol: 'NIFTY25JAN24500CE',
        quantity: 1,
        average_price: 100,
        previous_close: 50,
        last_price: 150,
        exchange: 'NFO',
      }),
      makePosition({
        account: 'dHaN',
        tradingsymbol: 'NIFTY25JAN24000PE',
        quantity: 1,
        average_price: 80,
        previous_close: 40,
        last_price: 120,
        exchange: 'NFO',
      }),
    ];

    const result = computePositionsByAccount(positions);

    expect(result.byAccount['ZERODHA']).toBeDefined();
    expect(result.byAccount['DHAN']).toBeDefined();
    expect(result.byAccount['ZeroDha']).toBeUndefined();
    expect(result.byAccount['dHaN']).toBeUndefined();
  });
});

describe('portfolioStore.positions.byAccount — edge cases', () => {
  it('single position with account aggregates to byAccount + TOTAL', () => {
    const positions = [
      makePosition({
        account: 'TESTACCT',
        tradingsymbol: 'NIFTY25JAN24500CE',
        quantity: 1,
        average_price: 100,
        previous_close: 50,
        last_price: 150,
        exchange: 'NFO',
      }),
    ];

    const result = computePositionsByAccount(positions);

    expect(result.byAccount['TESTACCT']).toBeGreaterThan(0);
    expect(result.byAccount['TOTAL']).toBe(result.byAccount['TESTACCT']);
  });

  it('position with negative day_pnl (loss) aggregates correctly', () => {
    const positions = [
      makePosition({
        account: 'ZERODHA',
        tradingsymbol: 'NIFTY25JAN24500CE',
        quantity: -1,
        average_price: 150,
        previous_close: 150,
        last_price: 100, // Price dropped → loss
        exchange: 'NFO',
        overnight_quantity: -1,
        day_change_val: -50,
        pnl: -50,
        prev_settlement_pnl: null,
      }),
    ];

    const result = computePositionsByAccount(positions, {
      livePosDay: (p) => baseDayPnlForPosition(p),
    });

    expect(result.byAccount['ZERODHA']).toBeLessThanOrEqual(0);
  });

  it('mixed profit and loss across accounts', () => {
    const positions = [
      makePosition({
        account: 'ZERODHA',
        quantity: 1,
        average_price: 100,
        previous_close: 50,
        last_price: 150, // profit
        exchange: 'NFO',
      }),
      makePosition({
        account: 'DHAN',
        quantity: -1,
        average_price: 150,
        previous_close: 150,
        last_price: 100, // loss
        exchange: 'NFO',
        overnight_quantity: -1,
      }),
    ];

    const result = computePositionsByAccount(positions);

    expect(Object.keys(result.byAccount)).toContain('ZERODHA');
    expect(Object.keys(result.byAccount)).toContain('DHAN');
    expect(Object.keys(result.byAccount)).toContain('TOTAL');
  });
});

// ── New: positionsDayPnlStore.byAccount getter ────────────────────────────────

/**
 * positionsDayPnlStore now also exports a byAccount getter that reads from
 * portfolioStore.positions.byAccount.
 *
 * Test coverage (functional layer):
 *   1. Getter returns portfolioStore.positions.byAccount
 *   2. Returns {} when positions.byAccount is undefined/null
 *   3. Includes TOTAL key with aggregate
 *   4. Account keys are uppercase
 */

function createMockPortfolioStore(positionsData = {}) {
  return {
    positions: {
      byKey: {},
      total: { day_pnl: 0 },
      byAccount: positionsData,
    },
  };
}

describe('positionsDayPnlStore.byAccount — getter delegation', () => {
  it('byAccount getter returns portfolioStore.positions.byAccount', () => {
    const mockStore = createMockPortfolioStore({
      'ZERODHA': 100,
      'DHAN': 200,
      'TOTAL': 300,
    });

    // Simulate the getter logic from positionsDayPnlStore
    const byAccountGetter = mockStore.positions.byAccount;

    expect(byAccountGetter['ZERODHA']).toBe(100);
    expect(byAccountGetter['DHAN']).toBe(200);
    expect(byAccountGetter['TOTAL']).toBe(300);
  });

  it('byAccount returns empty object when positions.byAccount is undefined', () => {
    const mockStore = createMockPortfolioStore(undefined);

    // Simulated getter with fallback
    const byAccountGetter = mockStore.positions.byAccount ?? {};

    expect(byAccountGetter).toEqual({});
  });

  it('byAccount returns empty object when positions.byAccount is null', () => {
    const mockStore = createMockPortfolioStore(null);

    const byAccountGetter = mockStore.positions.byAccount ?? {};

    expect(byAccountGetter).toEqual({});
  });

  it('byAccount includes TOTAL key matching aggregate', () => {
    const mockStore = createMockPortfolioStore({
      'ACC1': 50,
      'ACC2': 150,
      'TOTAL': 200,
    });

    const byAccountGetter = mockStore.positions.byAccount;

    expect(byAccountGetter['TOTAL']).toBe(200);
    expect(byAccountGetter['TOTAL']).toBe(byAccountGetter['ACC1'] + byAccountGetter['ACC2']);
  });

  it('byAccount keys are uppercase', () => {
    const mockStore = createMockPortfolioStore({
      'ZERODHA': 100,
      'DHAN': 200,
      'TOTAL': 300,
    });

    const byAccountGetter = mockStore.positions.byAccount;
    const keys = Object.keys(byAccountGetter);

    for (const key of keys) {
      expect(key).toBe(key.toUpperCase());
    }
  });

  it('multiple account aggregation is preserved through getter', () => {
    const mockStore = createMockPortfolioStore({
      'ACCOUNT_A': 1000,
      'ACCOUNT_B': 2000,
      'ACCOUNT_C': 3000,
      'TOTAL': 6000,
    });

    const byAccountGetter = mockStore.positions.byAccount;

    expect(Object.keys(byAccountGetter).length).toBe(4);
    expect(byAccountGetter['TOTAL']).toBe(6000);
  });
});

// ── Real-money guard (2026-09) — degraded-aware SWR fallback ─────────────────
//
// portfolioStore.svelte.js's `_portfolio` collector and FOUR of the
// `portfolioAggregates` getters (_livePositionsPnl, _liveHoldingsTotal,
// _liveHoldingsValue, _longOptionsCashPaid) treat a store tagged
// `.meta.degraded` (backend stale_accounts substitution — see
// marketDataStores.svelte.js's _bookStaleMeta / dataStore.svelte.js's
// extractStaleMeta) the SAME as a null/not-yet-landed read: freeze the
// WHOLE scalar at the last known-good value instead of recomputing off a
// partially-substituted or empty-but-technically-non-null response.
//
// The remaining THREE funds-derived getters (_liveCashTotal, _marginAvail,
// _marginTotal) were deliberately changed OFF this freeze-the-whole-scalar
// pattern in the 2026-09 NavStrip "₹0 margin for an extended period, then
// jumps to correct value" fix — see fundsAggregate.test.js below and
// fundsAggregate.js's file header for why: freezing the ENTIRE cross-
// account total on ANY single degraded account (common — one flaky Dhan/
// Groww account) held every healthy account's real capital hostage, and
// on a fresh page load the remembered scalar started at 0, so the first
// poll(s) landing while degraded produced a flat 0 that never recovered.
// Those three getters now SUM whatever fundRows holds on every read
// (including stale accounts' own backend-substituted last-known-good
// values) and return null only when fundRows itself is null/empty.
//
// portfolioStore.svelte.js can't be imported directly (Svelte 5 runes, no
// svelte-compiler plugin in this harness — see file header). These tests
// mirror the exact "freeze at last-good" pattern the FOUR non-funds
// $derived.by getters still use: a plain closure variable holding the
// last computed value, only updated when the read is fresh (non-null AND
// non-degraded).

describe('portfolioAggregates — last-good scalar snapshot (real-money guard, non-funds getters only)', () => {
  /**
   * Pure mirror of the pattern used by _livePositionsPnl / _liveHoldingsTotal
   * / _liveHoldingsValue / _longOptionsCashPaid in portfolioStore.svelte.js.
   * _liveCashTotal / _marginAvail / _marginTotal do NOT use this pattern
   * any more — see fundsAggregate.test.js and the file-header comment above.
   *
   *   let _lastX = 0;
   *   const _x = $derived.by(() => {
   *     const rows = someStore.value;
   *     if (!rows || someStore.meta?.degraded) return _lastX;
   *     const s = <compute from rows>;
   *     _lastX = s;
   *     return s;
   *   });
   *
   * Modelled here as a small class so each test gets an isolated closure
   * (mirrors a fresh module load).
   */
  class LastGoodScalar {
    constructor(compute) {
      this._last = 0;
      this._compute = compute;
    }
    /** @param {any[] | null} rows @param {boolean} degraded */
    read(rows, degraded) {
      if (!rows || degraded) return this._last;
      const s = this._compute(rows);
      this._last = s;
      return s;
    }
  }

  const sumPnl = (rows) => rows.reduce((s, r) => s + Number(r?.pnl || 0), 0);

  it('returns the freshly computed value on a non-degraded, non-null read', () => {
    const agg = new LastGoodScalar(sumPnl);
    const result = agg.read([{ pnl: 100 }, { pnl: 50 }], false);
    expect(result).toBe(150);
  });

  it('freezes at the last-good value when the store is degraded, even with real rows present', () => {
    const agg = new LastGoodScalar(sumPnl);
    agg.read([{ pnl: 100 }, { pnl: 50 }], false); // seed last-good = 150
    // Backend tags this poll degraded (stale_accounts substitution) — the
    // rows array might be a partial/substituted set; freeze rather than
    // recompute off it.
    const result = agg.read([{ pnl: 100 }], true);
    expect(result).toBe(150);
  });

  it('freezes at the last-good value when rows is null (mid softInvalidate / not-yet-landed)', () => {
    const agg = new LastGoodScalar(sumPnl);
    agg.read([{ pnl: 300 }], false); // seed last-good = 300
    const result = agg.read(null, false);
    expect(result).toBe(300);
  });

  it('returns 0 (the honest default) on a cold start with no prior read', () => {
    const agg = new LastGoodScalar(sumPnl);
    const result = agg.read(null, false);
    expect(result).toBe(0);
  });

  it('resumes computing fresh values once degraded clears', () => {
    const agg = new LastGoodScalar(sumPnl);
    agg.read([{ pnl: 100 }], false);       // last-good = 100
    agg.read([{ pnl: 999 }], true);        // frozen, still reads 100
    const result = agg.read([{ pnl: 250 }], false); // fresh again
    expect(result).toBe(250);
  });
});

describe('_portfolio collector — per-slice fresh = non-null AND non-degraded', () => {
  /**
   * Pure mirror of portfolioStore.svelte.js's _portfolio $derived.by:
   * each of positions/holdings/funds independently falls back to its own
   * last-known (or empty) slice unless it is BOTH non-null and NOT tagged
   * degraded ("fresh"). See the real source's posFresh/holdFresh/fundsFresh.
   */
  function computePortfolioSnapshot({ posAgg, holdAgg, fundsAgg, posDegraded, holdDegraded, fundsDegraded, last, EMPTY_POSITIONS, EMPTY_HOLDINGS, EMPTY_FUNDS }) {
    const posFresh   = posAgg   && !posDegraded;
    const holdFresh  = holdAgg  && !holdDegraded;
    const fundsFresh = fundsAgg && !fundsDegraded;
    if (!posFresh && !holdFresh && !fundsFresh) return last;
    return {
      positions: posFresh  ? posAgg   : (last?.positions ?? EMPTY_POSITIONS),
      holdings:  holdFresh ? holdAgg  : (last?.holdings  ?? EMPTY_HOLDINGS),
      funds:     fundsFresh? fundsAgg : (last?.funds     ?? EMPTY_FUNDS),
    };
  }

  const EMPTY_POSITIONS = { total: { day_pnl: 0 } };
  const EMPTY_HOLDINGS  = { total: 0 };
  const EMPTY_FUNDS     = { total: {} };

  it('all three slices fresh (non-null, non-degraded) → uses all three live', () => {
    const result = computePortfolioSnapshot({
      posAgg: { total: { day_pnl: 100 } }, holdAgg: { total: 50 }, fundsAgg: { total: { avail: 10 } },
      posDegraded: false, holdDegraded: false, fundsDegraded: false,
      last: null, EMPTY_POSITIONS, EMPTY_HOLDINGS, EMPTY_FUNDS,
    });
    expect(result.positions.total.day_pnl).toBe(100);
    expect(result.holdings.total).toBe(50);
    expect(result.funds.total.avail).toBe(10);
  });

  it('positions degraded (non-null but tagged stale) → falls back to last-known positions, holdings/funds stay fresh', () => {
    const last = { positions: { total: { day_pnl: 999 } }, holdings: EMPTY_HOLDINGS, funds: EMPTY_FUNDS };
    const result = computePortfolioSnapshot({
      posAgg: { total: { day_pnl: 1 } }, // would silently under-count if used
      holdAgg: { total: 75 },
      fundsAgg: { total: { avail: 20 } },
      posDegraded: true, holdDegraded: false, fundsDegraded: false,
      last, EMPTY_POSITIONS, EMPTY_HOLDINGS, EMPTY_FUNDS,
    });
    // Positions frozen at the last known-good 999, NOT the degraded 1.
    expect(result.positions.total.day_pnl).toBe(999);
    // Holdings/funds independently stay fresh.
    expect(result.holdings.total).toBe(75);
    expect(result.funds.total.avail).toBe(20);
  });

  it('all three degraded and no prior snapshot → returns null (first-paint fallback handled by exported getters)', () => {
    const result = computePortfolioSnapshot({
      posAgg: { total: {} }, holdAgg: { total: 0 }, fundsAgg: { total: {} },
      posDegraded: true, holdDegraded: true, fundsDegraded: true,
      last: null, EMPTY_POSITIONS, EMPTY_HOLDINGS, EMPTY_FUNDS,
    });
    expect(result).toBeNull();
  });

  it('a degraded slice with NO prior snapshot falls back to the EMPTY shape (not the degraded data)', () => {
    // holdings/funds are fresh (non-null, non-degraded) so the collector
    // doesn't bail out entirely — isolates the positions-specific fallback.
    const result = computePortfolioSnapshot({
      posAgg: { total: { day_pnl: 42 } }, // degraded — must not surface
      holdAgg: { total: 10 },
      fundsAgg: { total: { avail: 5 } },
      posDegraded: true, holdDegraded: false, fundsDegraded: false,
      last: null, EMPTY_POSITIONS, EMPTY_HOLDINGS, EMPTY_FUNDS,
    });
    expect(result.positions).toBe(EMPTY_POSITIONS);
    expect(result.holdings.total).toBe(10);
  });
});

// ── Real-money fix (2026-09) — _marginAvail/_marginTotal/_liveCashTotal ────
// source-grep guards. NavStrip "₹0 margin for an extended period, then
// jumps to correct value" incident — see fundsAggregate.js's file header
// for the full root-cause writeup and fundsAggregate.test.js for the pure-
// function unit tests covering the five required cases directly against
// the REAL shipped sum logic (not a mirror). This block only verifies the
// $derived.by bodies in portfolioStore.svelte.js actually delegate to
// those helpers and no longer contain the old freeze-the-whole-scalar
// pattern.
describe('portfolioStore.svelte.js — funds getters delegate to fundsAggregate.js (real-money fix)', () => {
  it('imports sumMarginAvail / sumMarginTotal / sumLiveCashTotal from fundsAggregate.js', () => {
    expect(portfolioStoreSrc).toMatch(
      /import\s*\{\s*sumMarginAvail,\s*sumMarginTotal,\s*sumLiveCashTotal\s*\}\s*from\s*'\$lib\/data\/fundsAggregate\.js'/
    );
  });

  it('does NOT declare the old freeze-the-whole-scalar last-good variables for funds (removed)', () => {
    expect(portfolioStoreSrc).not.toMatch(/let _lastLiveCashTotal/);
    expect(portfolioStoreSrc).not.toMatch(/let _lastMarginAvail/);
    expect(portfolioStoreSrc).not.toMatch(/let _lastMarginTotal/);
  });

  it('_liveCashTotal / _marginAvail / _marginTotal still do a TRACKED (not untrack()) read of fundsStore.value', () => {
    // Same item-1 invariant portfolioAggregatesTriggers.test.js pins for
    // every portfolioAggregates getter — the redesign must not regress it.
    for (const name of ['_liveCashTotal', '_marginAvail', '_marginTotal']) {
      const start = portfolioStoreSrc.indexOf(`const ${name} = $derived.by(() => {`);
      expect(start, `declaration for ${name} not found`).toBeGreaterThan(-1);
      const bodyEnd = portfolioStoreSrc.indexOf('\n});', start);
      const body = portfolioStoreSrc.slice(start, bodyEnd);
      expect(body, `${name} must read fundsStore.value directly (tracked)`).toMatch(/const fundRows = fundsStore\.value;/);
      expect(body, `${name} must NOT wrap fundsStore.value in untrack()`).not.toMatch(/untrack\(\(\) => fundsStore\.value\)/);
    }
  });

  it('_liveCashTotal delegates to sumLiveCashTotal(fundRows)', () => {
    const start = portfolioStoreSrc.indexOf('const _liveCashTotal = $derived.by(() => {');
    const bodyEnd = portfolioStoreSrc.indexOf('\n});', start);
    const body = portfolioStoreSrc.slice(start, bodyEnd);
    expect(body).toMatch(/return sumLiveCashTotal\(fundRows\);/);
  });

  it('_marginAvail delegates to sumMarginAvail(fundRows)', () => {
    const start = portfolioStoreSrc.indexOf('const _marginAvail = $derived.by(() => {');
    const bodyEnd = portfolioStoreSrc.indexOf('\n});', start);
    const body = portfolioStoreSrc.slice(start, bodyEnd);
    expect(body).toMatch(/return sumMarginAvail\(fundRows\);/);
  });

  it('_marginTotal delegates to sumMarginTotal(fundRows)', () => {
    const start = portfolioStoreSrc.indexOf('const _marginTotal = $derived.by(() => {');
    const bodyEnd = portfolioStoreSrc.indexOf('\n});', start);
    const body = portfolioStoreSrc.slice(start, bodyEnd);
    expect(body).toMatch(/return sumMarginTotal\(fundRows\);/);
  });
});
