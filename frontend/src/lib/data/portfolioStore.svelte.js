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
import { symbolTickCount, getSnapshot, liveSnap } from '$lib/data/symbolStore.svelte.js';
import { positionsStore, pulseHoldingsStore, fundsStore } from '$lib/data/marketDataStores.svelte.js';
import { baseDayPnlForPosition, dayChangePct } from '$lib/data/nav.js';
import { getUnderlyingSpot } from '$lib/data/underlyingSpotStore.svelte.js';
import { expiryPnl, expiryPnlWithRealised, resolveExpiryAnchor, legExtrinsicDisplay } from '$lib/data/expiryPnl.js';
import { decomposeSymbol } from '$lib/data/decomposeSymbol.js';
import { targetsForProxy, getProxyRow } from '$lib/data/hedgeProxies.js';
import { getInstrument } from '$lib/data/instruments';

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
  const posRows = positionsStore.value;
  if (!posRows) return null;
  return posRows.map(p => {
    const sym  = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
    return {
      ...p,
      _sym:        sym,
      // Poll-only LTP — positions/holdings Day P&L is purely poll-driven
      // (5s/30s book-poll cadence); no symbolStore/liveSnap tick read here.
      // Roots/underlyings (via _rootSpotCache below) remain tick-driven.
      _ltp:        Number(p?.last_price ?? 0),
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
  return _posTier1.map(p => {
    const isFO = FO_EXCHS.has(p._exch);
    // Poll-only — no live-tick delta (§1: positions Day P&L is purely
    // poll-driven; see baseDayPnlForPosition's baseline-diff formula).
    const day_pnl = baseDayPnlForPosition(p);
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
    if (isFO) {
      const decomp = decomposeSymbol(p._sym);
      const root   = (decomp.root || p._sym).toUpperCase();
      // Live front-month root spot is primary (tick-driven — the Exp P&L
      // SSOT for options); the polled `underlying_ltp` field is only a
      // fallback for the brief window before the first live root spot
      // lands. Reversed from the prior priority (polled-first) so Snapshot
      // Exp P&L / Legs TOTAL converge on the same source.
      const spot   = _rootSpotCache[root] || Number(p?.underlying_ltp || 0) || 0;
      const isCE   = p._sym.endsWith('CE'), isPE = p._sym.endsWith('PE');
      const isOpt  = isCE || isPE;
      // realised/pnl are passed through as-is — expiryPnlWithRealised applies
      // the pnl-fallback ONLY on its qty===0 branch (fully closed today);
      // pre-merging pnl into realised here would double-count against the
      // unrealised component already inside expiryPnl's intrinsic-value calc
      // for still-open legs.
      if (p._qty !== 0) {
        // Options value at the front-month root spot (matches the Exp P&L
        // column tooltip / Snapshot / Legs TOTAL). Futures value at THEIR
        // OWN contract's live price — a future's Exp P&L only equals the
        // root's front-month spot when the held contract IS the front-month
        // future; a far-month future must be valued on its own tick, not
        // the root's front-month resolution. liveSnap() is called directly
        // inside this $derived — safe per CLAUDE.md's reactive-safety rule
        // (it internally tracks snapTick and untrack-reads the map).
        const futLive = isOpt ? 0 : Number(liveSnap(p._sym)?.ltp || 0);
        const anchor = resolveExpiryAnchor({ isOpt, rootSpot: spot, ownLiveLtp: futLive, ownPolledLtp: p._ltp || 0 });
        if (anchor > 0) {
          const cRow = { symbol: p._sym, qty: p._qty, avg_cost: p._avg, ltp: p._ltp, kind: isOpt ? 'opt' : 'fut', realised: p?.realised, pnl: p?._pnl };
          const ev = expiryPnl(cRow, anchor);
          if (ev != null) {
            exp_pnl = expiryPnlWithRealised(cRow, anchor);
          }
          // Extrinsic (§7 + item-2 fix): delegates to the shared
          // legExtrinsicDisplay (expiryPnl.js) — the single implementation
          // also used by derivatives/+page.svelte's Snapshot/Legs Extrinsic
          // cells, so both surfaces stay identical by construction. That
          // helper (a) evaluates BOTH terms of the subtraction on the SAME
          // poll-time snapshot (c.ltp for MTM, the underlying's poll-time
          // spot — `p?.underlying_ltp` — for the exp-P&L term), never the
          // live-tick `anchor` above, closing the tick-vs-poll skew bug
          // (confirmed: ~₹2,000 phantom extrinsic on a CRUDEOIL future from
          // an unrelated spot tick landing between polls); (b) returns
          // `null` outright for futures — extrinsic ("time value") is an
          // options-only concept, not tautologically 0 for a linear
          // instrument valued at its own price. The live-tick `ev`/`exp_pnl`
          // above remain the Exp P&L column's DISPLAY value (correct,
          // intended per §4) — only Extrinsic needs the poll-consistent
          // basis.
          const pollAnchor = isOpt ? (Number(p?.underlying_ltp || 0) || 0) : 0;
          extrinsic = legExtrinsicDisplay(cRow, pollAnchor);
        }
      } else {
        exp_pnl = expiryPnlWithRealised({ symbol: p._sym, qty: 0, kind: isOpt ? 'opt' : 'fut', realised: p?.realised, pnl: p?._pnl }, null);
        // §7: closed futures have no time-value concept either — only
        // closed options settle to a well-defined "no time value left" 0.
        extrinsic = isOpt ? 0 : null;
      }
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
  const holdRows = pulseHoldingsStore.value;
  if (!holdRows) return null;
  return holdRows.map(h => {
    const sym  = String(h?.tradingsymbol || h?.symbol || '').toUpperCase();
    return {
      ...h,
      _sym:        sym,
      _prev_close: Number(h?.prev_close) || null,
      _held_qty:   Number(h?.quantity ?? 0),
      // Poll-only LTP — see _posTier1's matching comment (§1).
      _ltp:        Number(h?.last_price ?? 0),
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
        // untrack: getUnderlyingSpot() now reads symbolStore via getSnapshot()
        // internally (front-month resolution) — CLAUDE.md's reactive-safety
        // rule requires wrapping any getSnapshot()-backed read inside a
        // $derived, same as the sibling read at line ~67 already does.
        const targetSpot = untrack(() => getUnderlyingSpot(target));
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
// Partial fallback: as soon as ANY of positions/holdings/funds has landed,
// build a snapshot using that fresh slice + the last-known (or empty) value
// for whichever slice(s) haven't arrived yet — rather than blocking P:1/H:1
// display on the slowest of three independent fetches. Only when NONE has
// ever landed do we fall through to the previous full snapshot (or null on
// first paint, handled by the exported getters' `?? _EMPTY_*` fallback).
//
// Real-money guard (2026-09): a slice is ALSO treated as "not landed" when
// its source store is tagged degraded (positionsStore.meta.degraded /
// pulseHoldingsStore.meta.degraded / fundsStore.meta.degraded — see
// marketDataStores.svelte.js's _bookStaleMeta + dataStore.svelte.js's
// extractStaleMeta). Without this, a partially-degraded response (some
// accounts substituted, others empty) would still produce a non-null
// _posAgg/_holdAgg/_fundsAgg computed off the DEGRADED rows — silently
// under-counting the stale account's contribution instead of freezing at
// the last known-good full snapshot. Each slice degrades independently:
// a degraded positions fetch doesn't hold back a healthy holdings/funds
// update.
const _portfolio = $derived.by(() => {
  const posDegraded   = positionsStore.meta?.degraded === true;
  const holdDegraded  = pulseHoldingsStore.meta?.degraded === true;
  const fundsDegraded = fundsStore.meta?.degraded === true;
  const posFresh   = _posAgg   && !posDegraded;
  const holdFresh  = _holdAgg  && !holdDegraded;
  const fundsFresh = _fundsAgg && !fundsDegraded;
  if (!posFresh && !holdFresh && !fundsFresh) return _last;
  _last = {
    positions: posFresh ? {
      total:           _posAgg.posTotal,
      byKey:           _posAgg.posByKey,
      byAccount:       _posAgg.posByAccount,
      byRoot:          _posAgg.byRoot,
      byRootPositions: _posAgg.byRootPos,
      byRootHoldings:  _byRootHoldings,
      expiryByAcct:    _posAgg.expiryByAcct,
    } : (_last?.positions ?? _EMPTY_POSITIONS),
    holdings: holdFresh  ? _holdAgg   : (_last?.holdings ?? _EMPTY_HOLDINGS),
    funds:    fundsFresh ? _fundsAgg  : (_last?.funds    ?? _EMPTY_FUNDS),
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
      total:       _pulseHoldingsTotal,
      byKey:       _pulseHoldingsByKey ?? base.byKey,
      byAccount:   { ...base.byAccount, TOTAL: _pulseHoldingsTotal },
      chg_pct:     base.chg_pct,
      chgPctByKey: base.chgPctByKey,
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

// ── Cross-page portfolio aggregates (moved from PositionStrip) ────────────────
// Pre-computed totals that PositionStrip reads as $derived reflectors instead
// of re-deriving inline. Each does a TRACKED (non-untrack) read of the
// underlying store's .value (positionsStore/pulseHoldingsStore/fundsStore),
// so the derived re-runs whenever the poll/fill reload actually lands — the
// store's own .value assignment already IS the poll/fill/bookChanged signal,
// so a separate `void bookPollerTick.value; void _bookChangedTick;` pair was
// redundant (round-3 audit, cleaned up round 4) — removed. `void _tick;`
// (the SSE-tick throttle) was also removed — matches §1's poll-only
// unification for positions/holdings (Day P&L already went poll-only; these
// lifetime/value aggregates now consistently follow the same cadence rather
// than being the one remaining tick-reactive exception). Two of these
// (_liveHoldingsTotal/_liveHoldingsValue) still read the CURRENT live
// getSnapshot()/liveSnap() value at each poll-triggered recompute — they
// just no longer force a re-render on every intermediate SSE tick between
// polls, matching every sibling position/holding aggregate. Only
// `_rootSpotCache` above (root/underlying spot, intentionally still
// tick-driven per §1's carve-out) keeps its own `void _tick;`. Only
// per-symbol getSnapshot()/liveSnap() reads (raw Map lookups, not $state
// themselves) are wrapped in untrack(), per CLAUDE.md's reactive-safety
// rule.

// Real-money guard (2026-09) — last-good scalar snapshots for the
// portfolioAggregates getters below. Mirrors the `_last` object pattern
// already used by `_portfolio` above: a plain module-level variable
// (not $state) mutated inside each $derived.by, so a null/degraded read
// freezes at the last successfully-computed value instead of collapsing
// to 0. Addresses A4/R3: dataStore's softInvalidate()/invalidate() (and
// any transient null window between polls) used to null the underlying
// store value, and every one of these getters returned a bare 0 with no
// stale-while-revalidate guard — lifetime P&L, holdings value, cash,
// margin would all flash to 0 until the next poll landed.
let _lastLivePositionsPnl    = 0;
let _lastLiveHoldingsTotal   = 0;
let _lastLiveHoldingsValue   = 0;
let _lastLiveCashTotal       = 0;
let _lastLongOptionsCashPaid = 0;
let _lastMarginAvail         = 0;
let _lastMarginTotal         = 0;

// Sum of lifetime pnl across all position rows (P pill slot 2 in NavStrip).
// Reads raw broker pnl — no live-LTP delta — matching the MarketPulse TOTAL row
// which uses _broker_pnl (= Σ r.pnl) without an SSE delta.
const _livePositionsPnl = $derived.by(() => {
  // Tracked read — positionsStore.value is a $state getter, so this
  // derived re-runs whenever the poll/fill reload actually lands (the
  // store's own .set() IS the poll/fill signal — see the file-header
  // comment above this block). Wrapping this in untrack() was the root
  // cause of stale-until-next-poll aggregates in an earlier iteration.
  const posRows = positionsStore.value;
  // Degraded (backend-tagged substituted/stale accounts) or not-yet-
  // populated (null, e.g. mid softInvalidate) — freeze at last-good
  // rather than showing 0.
  if (!posRows || positionsStore.meta?.degraded) return _lastLivePositionsPnl;
  let s = 0;
  for (const p of posRows) s += Number(p?.pnl || 0);
  _lastLivePositionsPnl = s;
  return s;
});

// Live holdings total P&L: (liveHold − avg) × qty using symbolStore LTP.
// Matches MarketPulse mergeHoldingRows which uses live-LTP when available.
// Falls back to broker h.pnl when no live LTP is present.
const _liveHoldingsTotal = $derived.by(() => {
  // Tracked read — see _livePositionsPnl's comment above.
  const holdRows = pulseHoldingsStore.value;
  if (!holdRows || pulseHoldingsStore.meta?.degraded) return _lastLiveHoldingsTotal;
  let s = 0;
  for (const h of holdRows) {
    const sym      = String(h?.tradingsymbol || '').toUpperCase();
    const liveHold = untrack(() => getSnapshot(sym)?.ltp);
    const avgCost  = Number(h?.average_price || 0);
    const qty      = Number(h?.quantity || 0);
    if (liveHold != null && liveHold > 0 && avgCost > 0 && qty !== 0) {
      s += (liveHold - avgCost) * qty;
    } else {
      s += Number(h?.pnl || 0);
    }
  }
  _lastLiveHoldingsTotal = s;
  return s;
});

// Live holdings market value: ltp × qty (with fallback tiers matching PositionStrip).
// Tier 1: symbolStore ltp × qty. Tier 2: h.last_price × qty (avoids cur_val=inv_val trap).
// Tier 3: h.cur_val (broker computed, may equal inv_val when last_price=0).
const _liveHoldingsValue = $derived.by(() => {
  // Tracked read — see _livePositionsPnl's comment above.
  const holdRows = pulseHoldingsStore.value;
  if (!holdRows || pulseHoldingsStore.meta?.degraded) return _lastLiveHoldingsValue;
  let s = 0;
  for (const h of holdRows) {
    const sym    = String(h?.tradingsymbol || '').toUpperCase();
    const ltp    = untrack(() => getSnapshot(sym)?.ltp);
    const qty    = Number(h?.quantity || 0);
    const lastPx = Number(h?.last_price || 0);
    if (ltp != null && ltp > 0 && qty !== 0) {
      s += ltp * qty;
    } else if (lastPx > 0 && qty !== 0) {
      s += lastPx * qty;
    } else {
      s += Number(h?.cur_val || 0);
    }
  }
  _lastLiveHoldingsValue = s;
  return s;
});

// Live cash: Kite avail.cash (= live_balance) summed across all accounts.
// Falls back to f.cash if live_cash is not yet surfaced by the backend.
const _liveCashTotal = $derived.by(() => {
  // Tracked read — see _livePositionsPnl's comment above.
  const fundRows = fundsStore.value;
  if (!fundRows || fundsStore.meta?.degraded) return _lastLiveCashTotal;
  let s = 0;
  for (const f of fundRows) {
    const lc = Number(f?.live_cash ?? 0);
    s += lc !== 0 ? lc : Number(f?.cash || 0);
  }
  _lastLiveCashTotal = s;
  return s;
});

// Cash debited on currently-held long options.
// For each long CE/PE row: avg × lot_size × (qty / lot_size) = avg × qty.
// Using num_lots path for clarity; falls back to avg × qty if lot_size unavailable.
const _longOptionsCashPaid = $derived.by(() => {
  // Tracked read — see _livePositionsPnl's comment above.
  const posRows = positionsStore.value;
  if (!posRows || positionsStore.meta?.degraded) return _lastLongOptionsCashPaid;
  let s = 0;
  for (const p of posRows) {
    const sym = String(p?.tradingsymbol || '').toUpperCase();
    const isOpt = sym.endsWith('CE') || sym.endsWith('PE');
    const qty   = Math.abs(Number(p?.quantity) || 0);
    const avg   = Number(p?.average_price) || 0;
    if (!isOpt || Number(p?.quantity) <= 0) continue;
    const inst    = getInstrument(sym);
    const lotSize = Number(inst?.ls) || 0;
    if (lotSize > 0) {
      const numLots = qty / lotSize;
      s += avg * lotSize * numLots;
    } else {
      s += avg * qty;
    }
  }
  _lastLongOptionsCashPaid = s;
  return s;
});

// Margin available (deployable) across all accounts.
const _marginAvail = $derived.by(() => {
  // Tracked read — see _livePositionsPnl's comment above.
  const fundRows = fundsStore.value;
  if (!fundRows || fundsStore.meta?.degraded) return _lastMarginAvail;
  let s = 0;
  for (const f of fundRows) s += Number(f?.avail_margin || 0);
  _lastMarginAvail = s;
  return s;
});

// Margin total (used + available = full capacity) across all accounts.
const _marginTotal = $derived.by(() => {
  // Tracked read — see _livePositionsPnl's comment above.
  const fundRows = fundsStore.value;
  if (!fundRows || fundsStore.meta?.degraded) return _lastMarginTotal;
  let s = 0;
  for (const f of fundRows) {
    s += Number(f?.used_margin  || 0);
    s += Number(f?.avail_margin || 0);
  }
  _lastMarginTotal = s;
  return s;
});

/**
 * Pre-computed cross-page portfolio aggregates.
 * PositionStrip reads these as $derived reflectors instead of re-deriving inline.
 * All values gate on _tick (4Hz) — same reactive cadence as portfolioStore.
 */
export const portfolioAggregates = {
  /** Σ position.pnl — lifetime P&L total (NO live-LTP delta), matching MarketPulse TOTAL. */
  get livePositionsPnl()   { return _livePositionsPnl;    },
  /** Live holdings P&L: (ltp − avg) × qty per holding; falls back to broker h.pnl. */
  get liveHoldingsTotal()  { return _liveHoldingsTotal;   },
  /** Live holdings market value: ltp × qty (three-tier fallback). */
  get liveHoldingsValue()  { return _liveHoldingsValue;   },
  /** Available cash across all accounts (Kite live_balance, fallback to cash). */
  get liveCashTotal()      { return _liveCashTotal;       },
  /** Cash paid for currently-held long options (avg × qty via lot_size path). */
  get longOptionsCashPaid(){ return _longOptionsCashPaid; },
  /** Available margin across all accounts. */
  get marginAvail()        { return _marginAvail;         },
  /** Total margin capacity (used + available) across all accounts. */
  get marginTotal()        { return _marginTotal;         },
};
