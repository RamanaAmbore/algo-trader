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

// ── Root spot cache ──────────────────────────────────────────────────────────
const _rootSpotCache = $derived.by(() => {
  void _tick;
  const posRows = positionsStore.value;
  if (!posRows) return {};
  const cache = {};
  for (const p of posRows) {
    const sym  = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
    const exch = String(p?.exchange || '').toUpperCase();
    if (!FO_EXCHS.has(exch) || !sym) continue;
    const root = (decomposeSymbol(sym).root || sym).toUpperCase();
    if (root && !(root in cache)) {
      const live = untrack(() => getUnderlyingSpot(root));
      cache[root] = live > 0 ? live : (Number(p?.underlying_ltp) || 0);
    }
  }
  return cache;
});

// ── Tier 1 — raw + LTP ───────────────────────────────────────────────────────
const _posTier1 = $derived.by(() => {
  void _tick;
  const posRows = positionsStore.value;
  if (!posRows) return null;
  return posRows.map(p => {
    const sym  = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
    const snap = untrack(() => getSnapshot(sym));
    return {
      ...p,
      _sym:        sym,
      _ltp:        snap?.ltp ?? Number(p?.last_price ?? 0),
      _prev_close: Number(p?.prev_close) || null,
      _qty:        Number(p?.quantity ?? 0),
      _avg:        Number(p?.average_price ?? 0),
      _pnl:        Number(p?.pnl ?? 0),
      _exch:       String(p?.exchange || '').toUpperCase(),
    };
  });
});

// ── Tier 2 — prev_mv, day_pnl, F&O exp_pnl ──────────────────────────────────
const _posTier2 = $derived.by(() => {
  if (!_posTier1) return null;
  const marketOpen = isMarketOpen();
  return _posTier1.map(p => {
    const isFO = FO_EXCHS.has(p._exch);
    const day_pnl = livePositionDayPnl(
      { closePx: p._prev_close ?? 0, pollLtp: Number(p?.last_price ?? 0),
        qty: p._qty, avg: p._avg, dcvRow: p },
      p._ltp, { marketOpen }
    );
    // prev_mv: prev_close × |qty| for overnight positions.
    // For new intraday positions (oq=0, no prior session close), fall back to
    // avg_cost so chg_pct has a valid denominator (day_pnl / avg_cost × 100).
    // Overnight positions with prev_close=0 remain null — avg_cost ≠ prior close.
    const oq = Number(p?.overnight_quantity ?? 0);
    const prev_mv =
      p._prev_close != null && p._prev_close > 0 ? p._prev_close * Math.abs(p._qty)
      : oq === 0 && p._avg > 0                    ? p._avg       * Math.abs(p._qty)
      : null;
    if (prev_mv === null && oq !== 0)
      console.warn('[portfolioStore] prev_mv null:', p._sym, 'prev_close=', p._prev_close, 'oq=', oq);

    let exp_pnl = null, extrinsic = null;
    if (isFO && p._qty !== 0) {
      const decomp = decomposeSymbol(p._sym);
      const root   = (decomp.root || p._sym).toUpperCase();
      const spot   = Number(p?.underlying_ltp || 0) || _rootSpotCache[root] || 0;
      const isCE   = p._sym.endsWith('CE'), isPE = p._sym.endsWith('PE');
      const realised = Number(p?.realised ?? 0);
      let ev = null;
      if ((isCE || isPE) && spot > 0) {
        ev = expiryPnl({ symbol: p._sym, qty: p._qty, avg_cost: p._avg, kind: 'opt' }, spot);
      } else if (!isCE && !isPE) {
        const live = p._ltp || 0;
        if (live > 0) ev = expiryPnl({ symbol: p._sym, qty: p._qty, avg_cost: p._avg, kind: 'fut' }, live);
      }
      if (ev != null) { exp_pnl = ev + realised; extrinsic = ev - (p._ltp - p._avg) * p._qty; }
    } else if (isFO && p._qty === 0) {
      exp_pnl = Number(p?.realised || p?._pnl || 0); extrinsic = 0;
    }

    return { ...p, _day_pnl: day_pnl, _prev_mv: prev_mv, _isFO: isFO, _exp_pnl: exp_pnl, _extrinsic: extrinsic };
  });
});

// ── Tier 3 — chg_pct, root ───────────────────────────────────────────────────
const _posTier3 = $derived(
  _posTier2?.map(p => ({
    ...p,
    _chg_pct: p._prev_mv != null && p._prev_mv > 0
      ? p._day_pnl / p._prev_mv * 100 : null,
    _root: p._isFO
      ? (decomposeSymbol(p._sym).root || p._sym).toUpperCase()
      : p._sym,
  })) ?? null
);

// ── Position aggregates ───────────────────────────────────────────────────────
const _posAgg = $derived.by(() => {
  if (!_posTier3) return null;
  const posTotal      = { day_pnl: 0, exp_pnl: 0, extrinsic: 0, prev_mv: 0, chg_pct: null };
  const posByKey      = {};
  const posByAccount  = {};
  const byRootPos     = {};
  const byRoot        = {};
  const expiryByAcct  = new Map();

  for (const p of _posTier3) {
    if (!posByKey[p._sym]) posByKey[p._sym] = { day_pnl: 0, exp_pnl: null, extrinsic: null, pnl: 0, prev_mv: 0, chg_pct: null };
    const bk = posByKey[p._sym];
    bk.day_pnl += p._day_pnl;
    bk.pnl     += p._pnl;
    bk.prev_mv += p._prev_mv ?? 0;
    if (p._exp_pnl   != null) bk.exp_pnl   = (bk.exp_pnl   ?? 0) + p._exp_pnl;
    if (p._extrinsic != null) bk.extrinsic = (bk.extrinsic ?? 0) + p._extrinsic;

    posTotal.day_pnl += p._day_pnl;
    posTotal.prev_mv += p._prev_mv ?? 0;

    const _acct = String(p.account || '').toUpperCase();
    if (_acct) posByAccount[_acct] = (posByAccount[_acct] ?? 0) + p._day_pnl;
    if (p._exp_pnl   != null) posTotal.exp_pnl   += p._exp_pnl;
    if (p._extrinsic != null) posTotal.extrinsic += p._extrinsic;

    if (p._isFO) {
      const r = p._root;
      if (r) {
        if (!byRoot[r]) byRoot[r] = { spot: _rootSpotCache[r] || 0, legs: [], day_pnl: 0, exp_pnl: 0, extrinsic: 0, prev_mv: 0, chg_pct: null };
        byRoot[r].legs.push(p._sym);
        byRoot[r].day_pnl   += p._day_pnl;
        if (p._exp_pnl   != null) byRoot[r].exp_pnl   += p._exp_pnl;
        if (p._extrinsic != null) byRoot[r].extrinsic += p._extrinsic;
        byRoot[r].prev_mv   += p._prev_mv ?? 0;

        byRootPos[r] ??= { day_pnl: 0, exp_pnl: 0, extrinsic: 0, pnl: 0, prev_mv: 0, chg_pct: null };
        byRootPos[r].day_pnl   += p._day_pnl;
        byRootPos[r].pnl       += p._pnl;
        byRootPos[r].exp_pnl   += p._exp_pnl ?? 0;
        byRootPos[r].extrinsic += p._extrinsic ?? 0;
        byRootPos[r].prev_mv   += p._prev_mv ?? 0;

        if (p._exp_pnl != null) {
          const acct = String(p?.account || '');
          if (acct) expiryByAcct.set(acct, (expiryByAcct.get(acct) ?? 0) + p._exp_pnl);
        }
      }
    }
  }

  for (const bk of Object.values(posByKey)) {
    bk.chg_pct = bk.prev_mv > 0 ? dayChangePct(bk.day_pnl, bk.prev_mv) : null;
  }
  posTotal.chg_pct = posTotal.prev_mv > 0 ? dayChangePct(posTotal.day_pnl, posTotal.prev_mv) : null;
  for (const r of Object.values(byRoot)) {
    r.chg_pct = r.prev_mv > 0 ? dayChangePct(r.day_pnl, r.prev_mv) : null;
  }
  for (const r of Object.values(byRootPos)) {
    r.chg_pct = r.prev_mv > 0 ? dayChangePct(r.day_pnl, r.prev_mv) : null;
  }

  posByAccount['TOTAL'] = posTotal.day_pnl;
  return { posTotal, posByKey, posByAccount, byRoot, byRootPos, expiryByAcct };
});

// ── Holdings tiers ────────────────────────────────────────────────────────────
const _holdTier1 = $derived.by(() => {
  void _tick;
  const holdRows = pulseHoldingsStore.value;
  if (!holdRows) return null;
  return holdRows.map(h => {
    const sym  = String(h?.tradingsymbol || h?.symbol || '').toUpperCase();
    const snap = untrack(() => getSnapshot(sym));
    const snapLtp = snap?.ltp;
    return {
      ...h,
      _sym:        sym,
      _prev_close: Number(h?.prev_close) || null,
      _held_qty:   Number(h?.quantity ?? 0),
      _ltp:        (snapLtp != null && snapLtp > 0) ? Number(snapLtp) : Number(h?.last_price ?? 0),
      _dcv:        Number(h?.day_change_val) || 0,
    };
  });
});

const _holdTier2 = $derived(
  _holdTier1?.map(h => {
    const prev_mv = h._prev_close > 0 ? h._prev_close * Math.abs(h._held_qty) : null;
    let day_pnl;
    if (!h._prev_close || h._prev_close <= 0) {
      day_pnl = h._dcv;
    } else if (h._ltp > 0 && h._held_qty !== 0 && Math.abs(h._ltp - h._prev_close) > 0.005) {
      day_pnl = (h._ltp - h._prev_close) * h._held_qty;
    } else {
      day_pnl = h._dcv;
    }
    return { ...h, _prev_mv: prev_mv, _day_pnl: day_pnl };
  }) ?? null
);

const _holdTier3 = $derived(
  _holdTier2?.map(h => ({
    ...h,
    _chg_pct: h._prev_mv != null && h._prev_mv > 0
      ? h._day_pnl / h._prev_mv * 100 : null,
  })) ?? null
);

const _holdAgg = $derived.by(() => {
  if (!_holdTier3) return null;
  let total = 0, prev_mv = 0;
  const byKey = {}, byAccount = {}, chgPctByKey = {};
  for (const h of _holdTier3) {
    total   += h._day_pnl ?? 0;
    prev_mv += h._prev_mv ?? 0;
    byKey[h._sym] = (byKey[h._sym] ?? 0) + (h._day_pnl ?? 0);
    chgPctByKey[h._sym] = h._chg_pct;
    const acct = String(h?.account || '').toUpperCase();
    if (acct) byAccount[acct] = (byAccount[acct] ?? 0) + (h._day_pnl ?? 0);
  }
  byAccount['TOTAL'] = total;
  return { total, prev_mv, chg_pct: prev_mv > 0 ? dayChangePct(total, prev_mv) : null, byKey, chgPctByKey, byAccount };
});

// ── Cross-hedge byRootHoldings ─────────────────────────────────────────────
const _byRootHoldings = $derived.by(() => {
  if (!_holdTier1) return {};
  const map = {};
  for (const h of _holdTier1) {
    if (!h._held_qty || h._held_qty === 0) continue;
    const targets = targetsForProxy(h._sym);
    const credits = targets.length ? targets : [h._sym];
    for (const target of credits) {
      let exp_pnl = null;
      if (targets.length && h._ltp > 0) {
        const proxyRow   = getProxyRow(h._sym, target);
        const beta       = proxyRow?.beta ?? 1;
        const targetSpot = getUnderlyingSpot(target);
        if (targetSpot > 0) {
          const effQty = (beta * h._ltp * h._held_qty) / targetSpot;
          exp_pnl = (targetSpot - h._ltp / (beta || 1)) * effQty;
        }
      } else if (!targets.length && h._ltp > 0) {
        exp_pnl = (h._ltp - Number(h?.average_price ?? h?.avg_cost ?? 0)) * h._held_qty;
      }
      const r = map[target] ??= { day_pnl: 0, exp_pnl: 0, extrinsic: 0, pnl: 0 };
      r.pnl += Number(h?.pnl ?? 0);
      if (exp_pnl != null) r.exp_pnl += exp_pnl;
    }
  }
  return map;
});

// ── Funds aggregate ────────────────────────────────────────────────────────
const _fundsAgg = $derived.by(() => {
  const fundRows = fundsStore.value;
  if (!fundRows) return null;
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
  return fundsResult;
});

// ── Default shapes returned when deps are null (SWR fallback) ────────────────
const _EMPTY_POSITIONS = {
  total:          { day_pnl: 0, exp_pnl: 0, extrinsic: 0, prev_mv: 0, chg_pct: null },
  byKey:          {},
  byAccount:      {},
  byRootPositions:{},
  byRootHoldings: {},
  byRoot:         {},
  expiryByAcct:   new Map(),
};
const _EMPTY_HOLDINGS = { total: 0, byKey: {}, byAccount: {}, chg_pct: null, chgPctByKey: {} };
const _EMPTY_FUNDS    = { total: { live_cash: 0, avail_margin: 0, used_margin: 0, totalMargin: 0, utilPct: 0, collateral: 0 }, byAccount: {} };

// ── Final collector ────────────────────────────────────────────────────────
const _portfolio = $derived.by(() => {
  if (!_posAgg || !_holdAgg || !_fundsAgg) return _last;
  _last = {
    positions: {
      total:           _posAgg.posTotal,
      byKey:           _posAgg.posByKey,
      byAccount:       _posAgg.posByAccount,
      byRoot:          _posAgg.byRoot,
      byRootPositions: _posAgg.byRootPos,
      byRootHoldings:  _byRootHoldings,
      expiryByAcct:    _posAgg.expiryByAcct,
    },
    holdings: _holdAgg,
    funds:    _fundsAgg,
  };
  return _last;
});

/**
 * Unified portfolio store.
 *
 * All three sections are derived from a chained $derived tier structure with a
 * stale-while-revalidating (SWR) null-guard so consumers never see momentary
 * zero-out during a 5-second poll refresh.
 */
export const portfolioStore = {
  // ── Positions ────────────────────────────────────────────────────────────
  /** { total: {day_pnl,exp_pnl,extrinsic,prev_mv,chg_pct}, byKey, byRootPositions, byRootHoldings, byRoot, expiryByAcct } */
  get positions() { return _portfolio?.positions ?? _EMPTY_POSITIONS; },

  // ── Holdings (pulse-overridable) ─────────────────────────────────────────
  /**
   * { total: number, byKey: Record<string,number>, byAccount: Record<string,number>, chg_pct: number|null, chgPctByKey: Record<string,number|null> }
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
      chg_pct:   base.chg_pct,
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
