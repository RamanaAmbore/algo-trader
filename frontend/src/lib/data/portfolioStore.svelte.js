/**
 * portfolioStore — unified SWR (stale-while-revalidating) SSOT that absorbs
 * computation from positionsDerivedStore, holdingsDayPnlStore, and funds
 * aggregation into one reactive $derived.by().
 *
 * The SWR null-guard at the top of _portfolio prevents the 14 race-prone sites
 * where positionsStore / pulseHoldingsStore / fundsStore momentarily go null
 * during a 5s poll refresh, which used to cause derived values (chg%, day_pnl,
 * todayMtm, exp_pnl) to zero out. When any required dep is null, the last
 * known snapshot is returned instead of computing with empty arrays.
 *
 * Exports:
 *   portfolioStore.positions  — { total, byKey, byRootPositions, byRootHoldings, byRoot, expiryByAcct }
 *   portfolioStore.holdings   — { total, byKey, byAccount }  (pulse-overridable)
 *   portfolioStore.funds      — { total, byAccount }
 *   portfolioStore.setHoldingsFromPulse(byKey, total) — pulse override for MarketPulse
 *
 * Also exports _computeDerived as a named export for backward compat with any
 * future callers that want the pure function form (tests use local mirrors).
 */

import { browser } from '$app/environment';
import { untrack } from 'svelte';
import { symbolTickCount, getSnapshot } from '$lib/data/symbolStore.svelte.js';
import { positionsStore, pulseHoldingsStore, fundsStore } from '$lib/data/marketDataStores.svelte.js';
import { livePositionDayPnl, dayChangePct } from '$lib/data/nav.js';
import { isMarketOpen } from '$lib/marketHours';
import { getUnderlyingSpot } from '$lib/data/underlyingSpotStore.svelte.js';
import { expiryPnl } from '$lib/data/expiryPnl.js';
import { decomposeSymbol } from '$lib/data/decomposeSymbol.js';
import { targetsForProxy, getProxyRow } from '$lib/data/hedgeProxies.js';

const FO_EXCHS = new Set(['NFO', 'MCX', 'CDS', 'BFO']);

// ── 4 Hz throttle (250ms debounce on symbolTickCount) ───────────────────────
// Same pattern as positionsDerivedStore / holdingsDayPnlStore.
let _tick = $state(0);
/** @type {ReturnType<typeof setTimeout>|null} */
let _tickTimer = null;
if (browser) {
  symbolTickCount.subscribe(() => {
    if (_tickTimer) return;
    _tickTimer = setTimeout(() => { _tickTimer = null; _tick++; }, 250);
  });
}

// ── Pulse override state (holdings only) ────────────────────────────────────
// MarketPulse calls setHoldingsFromPulse() after each buildUnified pass so
// NavStrip H reads the cq-accurate per-symbol values from the grid.
let _pulseHoldingsTotal = $state(/** @type {number|null} */ (null));
let _pulseHoldingsByKey = $state(/** @type {Record<string,number>|null} */ (null));

// ── SWR last-known snapshot ──────────────────────────────────────────────────
/** @type {{ positions: any, holdings: any, funds: any }|null} */
let _last = null;

// ── Main computation ─────────────────────────────────────────────────────────
const _portfolio = $derived.by(() => {
  // Register throttled tick so this re-runs at most 4×/sec during SSE bursts.
  void _tick;

  // ── SWR NULL GUARD ────────────────────────────────────────────────────────
  // If any required dep is null (mid-poll refresh), return the last known
  // snapshot to prevent downstream derived values from zeroing out momentarily.
  const posRows  = positionsStore.value;
  const holdRows = pulseHoldingsStore.value;
  const fundRows = fundsStore.value;
  if (posRows == null || holdRows == null || fundRows == null) return _last;

  const marketOpen = isMarketOpen();

  // ── STEP 1: Positions (adapted from _computeDerived) ────────────────────

  const posTotal = { day_pnl: 0, exp_pnl: 0, extrinsic: 0 };
  /** @type {Record<string,{day_pnl:number,exp_pnl:number|null,extrinsic:number|null,pnl:number,prev_mv:number,chg_pct:number|null}>} */
  const posByKey = {};
  /** @type {Record<string,{day_pnl:number,exp_pnl:number,extrinsic:number,pnl:number}>} */
  const byRootPositions = {};
  /** @type {Record<string,{day_pnl:number,exp_pnl:number,extrinsic:number,pnl:number}>} */
  const byRootHoldings  = {};
  /** @type {Record<string,{spot:number,legs:string[],day_pnl:number,exp_pnl:number,extrinsic:number}>} */
  const byRoot = {};
  /** @type {Map<string,number>} */
  const expiryByAcct = new Map();

  // Build root→spot map ONCE before the positions loop so getUnderlyingSpot
  // is only called once per root (not once per leg). All reads are wrapped in
  // untrack() so individual symbol ticks don't register as reactive deps here —
  // the throttled _tick drives recompute instead.
  /** @type {Record<string,number>} */
  const rootSpotCache = {};
  for (const p of posRows) {
    const sym  = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
    if (!sym) continue;
    const exch = String(p?.exchange || '').toUpperCase();
    if (!FO_EXCHS.has(exch)) continue;
    const decomp = decomposeSymbol(sym);
    const root   = (decomp.root || sym).toUpperCase();
    if (root && !(root in rootSpotCache)) {
      const liveSpot = untrack(() => getUnderlyingSpot(root));
      rootSpotCache[root] = liveSpot > 0 ? liveSpot : (Number(p?.underlying_ltp) || 0);
    }
  }

  for (const p of posRows) {
    const sym = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
    if (!sym) continue;

    const qty  = Number(p?.quantity ?? 0) || 0;
    const avg  = Number(p?.average_price ?? 0) || 0;
    const pnl  = Number(p?.pnl ?? 0);
    // untrack: individual symbol ticks must not register as per-sym reactive deps.
    const snap = untrack(() => getSnapshot(sym));
    const ltp  = snap?.ltp ?? Number(p?.last_price ?? 0);

    const day_pnl = livePositionDayPnl(
      {
        closePx: Number(p?.previous_close) || Number(p?.close_price ?? 0),
        pollLtp: Number(p?.last_price      ?? 0),
        qty:     Number(p?.quantity        ?? 0),
        avg:     Number(p?.average_price   ?? 0),
        dcvRow:  p,
      },
      ltp,
      { marketOpen },
    );

    const exch = String(p?.exchange || '').toUpperCase();
    const isFO = FO_EXCHS.has(exch);

    let exp_pnl   = null;
    let extrinsic = null;
    let expVal    = null;

    if (isFO) {
      const realised = Number(p?.realised ?? 0) || 0;

      if (qty === 0) {
        // Closed leg — realised is locked in, no spot math needed.
        exp_pnl   = Number(p?.realised || p?.pnl || 0);
        extrinsic = 0;
        expVal    = exp_pnl;
      } else {
        const isCE = sym.endsWith('CE');
        const isPE = sym.endsWith('PE');

        let ev = null;
        if (isCE || isPE) {
          // Option intrinsic: use root spot from cache (pre-built above).
          const decomp  = decomposeSymbol(sym);
          const root    = (decomp.root || sym).toUpperCase();
          const spot1   = Number(p?.underlying_ltp || 0);
          const spot    = spot1 > 0 ? spot1 : (rootSpotCache[root] || 0);
          if (spot > 0) {
            ev = expiryPnl({ symbol: sym, qty, avg_cost: avg, kind: 'opt' }, spot);
          }
        } else {
          // Futures — own LTP is the "expiry" value.
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

    if (!posByKey[sym]) posByKey[sym] = { day_pnl: 0, exp_pnl: null, extrinsic: null, pnl: 0, prev_mv: 0, chg_pct: null };
    const bk = posByKey[sym];
    bk.day_pnl += day_pnl;
    bk.pnl     += pnl;
    const prev_close = Number(p?.previous_close) || Number(p?.close_price) || 0;
    const refPx      = prev_close > 0 ? prev_close : avg;
    bk.prev_mv       = (bk.prev_mv || 0) + refPx * Math.abs(qty);
    if (exp_pnl   != null) bk.exp_pnl   = (bk.exp_pnl   ?? 0) + exp_pnl;
    if (extrinsic != null) bk.extrinsic = (bk.extrinsic ?? 0) + extrinsic;

    posTotal.day_pnl += day_pnl;
    if (exp_pnl   != null) posTotal.exp_pnl   += exp_pnl;
    if (extrinsic != null) posTotal.extrinsic += extrinsic;

    if (isFO && expVal != null) {
      const acct = String(p?.account || '');
      if (acct) expiryByAcct.set(acct, (expiryByAcct.get(acct) ?? 0) + expVal);

      const decomp = decomposeSymbol(sym);
      const root   = (decomp.root || sym).toUpperCase();
      if (root) {
        // byRootPositions — same shape as the existing _computeDerived output,
        // used by derivatives Snapshot TOTAL sums.
        const rp = byRootPositions[root] ??= { day_pnl: 0, exp_pnl: 0, extrinsic: 0, pnl: 0 };
        rp.day_pnl   += day_pnl;
        rp.pnl       += pnl;
        rp.exp_pnl   += (exp_pnl   ?? 0);
        rp.extrinsic += (extrinsic ?? 0);

        // byRoot — new aggregated map with legs + spot for payoff callers.
        if (!byRoot[root]) byRoot[root] = { spot: rootSpotCache[root] || 0, legs: [], day_pnl: 0, exp_pnl: 0, extrinsic: 0 };
        byRoot[root].legs.push(sym);
        byRoot[root].day_pnl += day_pnl;
        if (exp_pnl   != null) byRoot[root].exp_pnl   += exp_pnl;
        if (extrinsic != null) byRoot[root].extrinsic += extrinsic;
      }
    }
  }

  // ── Holdings cross-hedge loop (byRootHoldings) ──────────────────────────
  // Mirrors the holdRows loop in _computeDerived for cross-hedge attribution.
  // This is SEPARATE from STEP 2's holdings day P&L loop — different purpose,
  // different output shape.
  for (const h of holdRows) {
    const sym = String(h?.tradingsymbol || h?.symbol || '').toUpperCase();
    if (!sym) continue;

    const qty  = Number(h?.quantity) || 0;
    const cost = Number(h?.average_price ?? h?.avg_cost) || 0;
    // untrack: same reason as positions loop above.
    const snapH = untrack(() => getSnapshot(sym));
    const ltp   = (snapH?.ltp ?? 0) > 0 ? Number(snapH.ltp) : Number(h?.last_price ?? 0);

    if (qty === 0) continue;

    const targets = targetsForProxy(sym);
    const credits = targets.length ? targets : [sym];

    for (const target of credits) {
      let exp_pnl = null;

      if (targets.length && ltp > 0) {
        // Beta-adjusted cross-hedge contribution.
        const proxyRow   = getProxyRow(sym, target);
        const beta       = proxyRow?.beta ?? 1;
        const targetSpot = getUnderlyingSpot(target);
        if (targetSpot > 0) {
          const effQty = (beta * ltp * qty) / targetSpot;
          exp_pnl = (targetSpot - ltp / (beta || 1)) * effQty;
        }
      } else if (!targets.length && ltp > 0) {
        // Direct equity: (ltp − cost) × qty.
        exp_pnl = (ltp - cost) * qty;
      }

      const r = byRootHoldings[target] ??= { day_pnl: 0, exp_pnl: 0, extrinsic: 0, pnl: 0 };
      r.pnl += Number(h?.pnl ?? 0);
      if (exp_pnl != null) r.exp_pnl += exp_pnl;
    }
  }

  // Compute chg_pct once per symbol key — all surfaces read the same value.
  for (const bk of Object.values(posByKey)) {
    bk.chg_pct = dayChangePct(bk.day_pnl, bk.prev_mv);
  }

  // ── STEP 2: Holdings day P&L (from holdingsDayPnlStore) ─────────────────
  const holdingsResult = { total: 0, byKey: /** @type {Record<string,number>} */ ({}), byAccount: /** @type {Record<string,number>} */ ({}) };
  for (const h of holdRows) {
    const sym = String(h?.tradingsymbol || h?.symbol || '').toUpperCase();
    if (!sym) continue;

    // untrack: per-symbol reads must not register as per-sym reactive deps.
    const snap    = untrack(() => getSnapshot(sym));
    const snapLtp = snap?.ltp;

    // Prefer snapshot LTP; fall back to broker last_price for symbols
    // not subscribed on the ticker (equity holdings off watchlist).
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
      // Live formula — mirrors _liveHoldingsToday and mergeHoldingRows.
      val = (liveLtp - closePx) * heldQty;
    } else {
      // Market closed or price flat (ltp ≈ close): fall back to broker day_change_val.
      val = dcv;
    }

    holdingsResult.byKey[sym] = (holdingsResult.byKey[sym] ?? 0) + val;
    holdingsResult.total += val;

    const acc = String(h?.account || '').toUpperCase();
    if (acc) {
      if (!holdingsResult.byAccount[acc]) holdingsResult.byAccount[acc] = 0;
      holdingsResult.byAccount[acc] += val;
    }
  }
  holdingsResult.byAccount['TOTAL'] = holdingsResult.total;

  // ── STEP 3: Funds aggregation ────────────────────────────────────────────
  const fundsResult = {
    total: { live_cash: 0, avail_margin: 0, used_margin: 0, totalMargin: 0, utilPct: 0, collateral: 0 },
    byAccount: /** @type {Record<string,any>} */ ({}),
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
    fundsResult.byAccount[acct] = { live_cash, avail_margin, used_margin, collateral, totalMargin, utilPct };
    fundsResult.total.live_cash    += live_cash;
    fundsResult.total.avail_margin += avail_margin;
    fundsResult.total.used_margin  += used_margin;
    fundsResult.total.collateral   += collateral;
  }
  fundsResult.total.totalMargin = fundsResult.total.used_margin + fundsResult.total.avail_margin;
  fundsResult.total.utilPct = fundsResult.total.totalMargin > 0
    ? (fundsResult.total.used_margin / fundsResult.total.totalMargin) * 100 : 0;

  _last = {
    positions: { total: posTotal, byKey: posByKey, byRootPositions, byRootHoldings, byRoot, expiryByAcct },
    holdings:  holdingsResult,
    funds:     fundsResult,
  };
  return _last;
});

// ── Default shapes returned when deps are null (SWR fallback) ────────────────
const _EMPTY_POSITIONS = {
  total:          { day_pnl: 0, exp_pnl: 0, extrinsic: 0 },
  byKey:          {},
  byRootPositions:{},
  byRootHoldings: {},
  byRoot:         {},
  expiryByAcct:   new Map(),
};
const _EMPTY_HOLDINGS = { total: 0, byKey: {}, byAccount: {} };
const _EMPTY_FUNDS    = { total: { live_cash: 0, avail_margin: 0, used_margin: 0, totalMargin: 0, utilPct: 0, collateral: 0 }, byAccount: {} };

/**
 * Unified portfolio store.
 *
 * All three sections are derived from a single $derived.by() block with a
 * stale-while-revalidating (SWR) null-guard so consumers never see momentary
 * zero-out during a 5-second poll refresh.
 */
export const portfolioStore = {
  // ── Positions ────────────────────────────────────────────────────────────
  /** { total: {day_pnl,exp_pnl,extrinsic}, byKey, byRootPositions, byRootHoldings, byRoot, expiryByAcct } */
  get positions() { return _portfolio?.positions ?? _EMPTY_POSITIONS; },

  // ── Holdings (pulse-overridable) ─────────────────────────────────────────
  /**
   * { total: number, byKey: Record<string,number>, byAccount: Record<string,number> }
   *
   * MarketPulse calls setHoldingsFromPulse() after each buildUnified pass so
   * NavStrip H reflects cq-accurate filter-aware values from the grid.
   * byAccount per-account keys always come from the base computation; only
   * TOTAL + byKey are overridden when a pulse value is active.
   */
  get holdings() {
    const base = _portfolio?.holdings ?? _EMPTY_HOLDINGS;
    if (_pulseHoldingsTotal === null) return base;
    return {
      total:     _pulseHoldingsTotal,
      byKey:     _pulseHoldingsByKey ?? base.byKey,
      byAccount: { ...base.byAccount, TOTAL: _pulseHoldingsTotal },
    };
  },

  // ── Funds ─────────────────────────────────────────────────────────────────
  /** { total: {live_cash,avail_margin,used_margin,...}, byAccount: Record<string,{...}> } */
  get funds() { return _portfolio?.funds ?? _EMPTY_FUNDS; },

  /**
   * Called by MarketPulse after each buildUnified with cq-accurate per-symbol
   * and aggregate values. Mirrors holdingsDayPnlStore.setFromPulse.
   * @param {Record<string,number>} byKey
   * @param {number} total
   */
  setHoldingsFromPulse(byKey, total) {
    _pulseHoldingsByKey  = byKey;
    _pulseHoldingsTotal  = total;
  },
};

/**
 * Pure computation — exported as a named function for backward compat.
 *
 * The Vitest tests for positionsDerivedStore use local mirrors of this logic,
 * not this import, so this export is provided as a safety net for any future
 * direct callers. The original signature is preserved exactly.
 *
 * @param {any[]} posRows
 * @param {any[]} holdRows
 * @param {object} [deps] - injectable for testing
 */
export function _computeDerived(posRows, holdRows, deps = {}) {
  const {
    getSnap    = sym  => untrack(() => getSnapshot(sym)),
    getSpot    = root => getUnderlyingSpot(root),
    getTargets = sym  => targetsForProxy(sym),
    getProxy   = (sym, tgt) => getProxyRow(sym, tgt),
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
    marketOpen = isMarketOpen(),
  } = deps;

  const total = { day_pnl: 0, exp_pnl: 0, extrinsic: 0 };
  /** @type {Record<string,{day_pnl:number,exp_pnl:number|null,extrinsic:number|null,pnl:number,prev_mv:number,chg_pct:number|null}>} */
  const byKey = {};
  /** @type {Record<string,{day_pnl:number,exp_pnl:number,extrinsic:number,pnl:number}>} */
  const byRootPositions = {};
  /** @type {Record<string,{day_pnl:number,exp_pnl:number,extrinsic:number,pnl:number}>} */
  const byRootHoldings  = {};
  /** @type {Map<string,number>} */
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
          const decomp  = decomposeSymbol(sym);
          const root    = (decomp.root || sym).toUpperCase();
          const spot1   = Number(p?.underlying_ltp || 0);
          const spot    = spot1 > 0 ? spot1 : getSpot(root);
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

    if (!byKey[sym]) byKey[sym] = { day_pnl: 0, exp_pnl: null, extrinsic: null, pnl: 0, prev_mv: 0, chg_pct: null };
    const bk = byKey[sym];
    bk.day_pnl += day_pnl;
    bk.pnl     += pnl;
    const prev_close = Number(p?.previous_close) || Number(p?.close_price) || 0;
    const refPx      = prev_close > 0 ? prev_close : avg;
    bk.prev_mv       = (bk.prev_mv || 0) + refPx * Math.abs(qty);
    if (exp_pnl   != null) bk.exp_pnl   = (bk.exp_pnl   ?? 0) + exp_pnl;
    if (extrinsic != null) bk.extrinsic = (bk.extrinsic ?? 0) + extrinsic;

    total.day_pnl += day_pnl;
    if (exp_pnl   != null) total.exp_pnl   += exp_pnl;
    if (extrinsic != null) total.extrinsic += extrinsic;

    if (isFO && expVal != null) {
      const acct = String(p?.account || '');
      if (acct) expiryByAcct.set(acct, (expiryByAcct.get(acct) ?? 0) + expVal);

      const decomp = decomposeSymbol(sym);
      const root   = (decomp.root || sym).toUpperCase();
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
    bk.chg_pct = dayChangePct(bk.day_pnl, bk.prev_mv);
  }

  return { total, byKey, byRootPositions, byRootHoldings, expiryByAcct };
}
