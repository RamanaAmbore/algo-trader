/**
 * positionsDerivedStore — unified SSOT for Day P&L, Exp P&L, and Extrinsic
 * across all surfaces (NavStrip, Pulse, Legs, Snapshot).
 *
 * Runs at 4 Hz (symbolTickCount + 250ms throttle) — same cadence as the
 * old positionsDayPnlStore. Expiry math uses expiryPnl.js (SSOT formula).
 *
 * setFromPulse() is retained as a no-op so MarketPulse imports compile
 * without changes — all values now come from reactive computation only.
 */

import { browser } from '$app/environment';
import { untrack } from 'svelte';
import { symbolTickCount, getSnapshot } from '$lib/data/symbolStore.svelte.js';
import { positionsStore, pulseHoldingsStore } from '$lib/data/marketDataStores.svelte.js';
import { livePositionDayPnl, dayChangePct } from '$lib/data/nav.js';
import { isMarketOpen } from '$lib/marketHours';
import { getUnderlyingSpot } from '$lib/data/underlyingSpotStore.svelte.js';
import { expiryPnl } from '$lib/data/expiryPnl.js';
import { decomposeSymbol } from '$lib/data/decomposeSymbol.js';
import { targetsForProxy, getProxyRow } from '$lib/data/hedgeProxies.js';

const FO_EXCHS = new Set(['NFO', 'MCX', 'CDS', 'BFO']);

let _tick = $state(0);
/** @type {ReturnType<typeof setTimeout>|null} */
let _tickTimer = null;
if (browser) {
  symbolTickCount.subscribe(() => {
    if (_tickTimer) return;
    _tickTimer = setTimeout(() => { _tickTimer = null; _tick++; }, 250);
  });
}

/**
 * Pure computation — exported for Vitest.
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

  // ── Positions ────────────────────────────────────────────────────────────
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
    let expVal    = null;   // raw expiryPnl() result (without realised)

    if (isFO) {
      const realised = Number(p?.realised ?? 0) || 0;

      if (qty === 0) {
        // Closed leg — realised is locked in, no spot math needed
        exp_pnl   = Number(p?.realised || p?.pnl || 0);
        extrinsic = 0;
        expVal    = exp_pnl;
      } else {
        const isCE = sym.endsWith('CE');
        const isPE = sym.endsWith('PE');

        let ev = null;
        if (isCE || isPE) {
          // Option intrinsic
          const decomp  = decomposeSymbol(sym);
          const root    = (decomp.root || sym).toUpperCase();
          const spot1   = Number(p?.underlying_ltp || 0);
          const spot    = spot1 > 0 ? spot1 : getSpot(root);
          if (spot > 0) {
            ev = expiryPnl({ symbol: sym, qty, avg_cost: avg, kind: 'opt' }, spot);
          }
        } else {
          // Futures — own LTP is the "expiry" value
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

  // ── Holdings (cross-hedge) ───────────────────────────────────────────────
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
        // Beta-adjusted cross-hedge contribution
        const proxyRow   = getProxy(sym, target);
        const beta       = proxyRow?.beta ?? 1;
        const targetSpot = getSpot(target);
        if (targetSpot > 0) {
          const effQty = (beta * ltp * qty) / targetSpot;
          exp_pnl = (targetSpot - ltp / (beta || 1)) * effQty;
        }
      } else if (!targets.length && ltp > 0) {
        // Direct equity: (ltp − cost) × qty
        exp_pnl = (ltp - cost) * qty;
      }

      const r = byRootHoldings[target] ??= { day_pnl: 0, exp_pnl: 0, extrinsic: 0, pnl: 0 };
      r.pnl += Number(h?.pnl ?? 0);
      if (exp_pnl != null) r.exp_pnl += exp_pnl;
    }
  }

  // Compute chg_pct once per symbol key so all surfaces read the same value.
  for (const bk of Object.values(byKey)) {
    bk.chg_pct = dayChangePct(bk.day_pnl, bk.prev_mv);
  }

  return { total, byKey, byRootPositions, byRootHoldings, expiryByAcct };
}

const _computed = $derived.by(() => {
  void _tick;
  const posRows  = positionsStore.value    ?? [];
  const holdRows = pulseHoldingsStore.value ?? [];
  return _computeDerived(posRows, holdRows);
});

export const positionsDerivedStore = {
  /** { day_pnl, exp_pnl, extrinsic } — aggregate totals */
  get total()           { return _computed.total;           },
  /** Alias — same as total.exp_pnl, for consumers that used the old expiryTotal */
  get expiryTotal()     { return _computed.total.exp_pnl;   },
  /** { [sym]: { day_pnl, exp_pnl, extrinsic, pnl } } */
  get byKey()           { return _computed.byKey;           },
  /** Map<account, expiry P&L> — for NavBreakdown P slot */
  get expiryByAcct()    { return _computed.expiryByAcct;    },
  /** { [root]: { day_pnl, exp_pnl, extrinsic, pnl } } — for Snapshot */
  get byRootPositions() { return _computed.byRootPositions; },
  /** { [root]: { exp_pnl, ... } } — for Snapshot Hold toggle */
  get byRootHoldings()  { return _computed.byRootHoldings;  },

  // no-op: MarketPulse used to override day P&L via this. Now the store is
  // the sole SSOT — Pulse no longer needs to push overrides.
  setFromPulse() {},
};
