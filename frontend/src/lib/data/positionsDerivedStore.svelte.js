/**
 * positionsDerivedStore — unified module-level singleton that is the single
 * source of truth for three related aggregates across all surfaces:
 *
 *   dayTotal       — live Day P&L across all F&O + equity positions (4 Hz)
 *   expiryTotal    — expiry-scenario P&L (intrinsic for options, spot-cost
 *                    for futures; closed-leg realised locked in)
 *   byRootPositions — { ROOT: { day, expiry } } for F&O positions (Snapshot)
 *   byRootHoldings  — { ROOT: { expiry } } for equity holdings (Snapshot Hold toggle)
 *
 * Replaces the split positionsDayPnlStore / PositionStrip._expiryProfit /
 * derivatives._byUnderlyingExp family.  One $derived.by() that runs at 4 Hz
 * (symbolTickCount + 250ms throttle, same cadence as positionsDayPnlStore).
 *
 * Pattern: module-local $state, no exported reassigned binding (Svelte 5
 * forbids exporting a reassigned $state). Read-only handle exposed via
 * `positionsDerivedStore.value`.
 *
 * setFromPulse() override — MarketPulse writes cq-accurate per-symbol and
 * aggregate values after each buildUnified pass.  These take priority over
 * the SSE-derived computation (identical contract to positionsDayPnlStore).
 *
 * Spot resolution for expiry P&L (positions):
 *   Priority 1: p.underlying_ltp (backend-stamped, positions.py Pass 3 — SSOT)
 *   Priority 2: underlyingSpotStore (shared batchQuote result every 30s)
 *   No further SSE fallback — avoids mis-keyed MCX option LTP as spot proxy.
 *
 * Expiry semantics per position row (mirrors PositionStrip._expiryForPosition):
 *   - Non-derivative exchanges (NSE/BSE) → null (excluded from expiry total)
 *   - qty === 0 (closed leg) → realised = p.pnl (no spot needed)
 *   - CE / PE → option intrinsic via expiryPnl({ kind:'opt' }, spot)
 *   - FUT / other F&O → futures formula via expiryPnl({ kind:'fut' }, live)
 *   - v != null → v + Number(p.realised || 0)
 */

import { browser } from '$app/environment';
import { untrack } from 'svelte';
import { symbolTickCount, getSnapshot } from '$lib/data/symbolStore.svelte.js';
import { positionsStore, pulseHoldingsStore } from '$lib/data/marketDataStores.svelte.js';
import { livePositionDayPnl } from '$lib/data/nav.js';
import { isMarketOpen } from '$lib/marketHours';
import { getUnderlyingSpot } from '$lib/data/underlyingSpotStore.svelte.js';
import { expiryPnl } from '$lib/data/expiryPnl.js';
import { decomposeSymbol } from '$lib/data/decomposeSymbol.js';
import { targetsForProxy } from '$lib/data/hedgeProxies.js';

// ---------------------------------------------------------------------------
// 4 Hz throttle — mirrors positionsDayPnlStore / holdingsDayPnlStore pattern.
// Wrapped in browser guard: SSR has no setTimeout and no symbolTickCount.
// ---------------------------------------------------------------------------
let _tick = $state(0);
/** @type {ReturnType<typeof setTimeout> | null} */
let _tickTimer = null;

if (browser) {
  symbolTickCount.subscribe(() => {
    if (_tickTimer) return;
    _tickTimer = setTimeout(() => {
      _tickTimer = null;
      _tick++;
    }, 250);
  });
}

// ---------------------------------------------------------------------------
// Pulse override — written by MarketPulse after each buildUnified.
// null = no override yet; store falls back to _computed values.
// ---------------------------------------------------------------------------
let _pulseTotal = $state(/** @type {number|null} */ (null));
let _pulseByKey = $state(/** @type {Record<string,number>|null} */ (null));

// ---------------------------------------------------------------------------
// Derivative-exchange gate — same set as FO_EXCHANGES in nav.js
// ---------------------------------------------------------------------------
const FO_EXCHS = new Set(['NFO', 'MCX', 'CDS', 'BFO']);

/**
 * Resolve spot for an option/futures leg.
 *   Priority 1: backend-stamped underlying_ltp (positions.py Pass 3)
 *   Priority 2: underlyingSpotStore (shared batchQuote, updated every 30s)
 *
 * @param {any} p   - raw position row
 * @param {string} root  - underlying root (e.g. "NIFTY", "CRUDEOIL")
 * @returns {number}
 */
function _resolveSpot(p, root) {
  const spot1 = Number(p?.underlying_ltp || 0);
  if (spot1 > 0) return spot1;
  return getUnderlyingSpot(root);
}

/**
 * Per-position expiry P&L contribution.
 * Mirrors PositionStrip._expiryForPosition exactly (closed-leg, realised,
 * spot-priority, equity gate).
 *
 * @param {any} p  - raw position row
 * @returns {number | null}
 */
function _expiryForPosition(p) {
  const exch = String(p?.exchange || '').toUpperCase();
  if (!FO_EXCHS.has(exch)) return null;  // equity NSE/BSE → excluded

  const sym = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
  const qty = Number(p?.quantity ?? p?.qty) || 0;
  const avg = Number(p?.average_price ?? p?.avg_cost) || 0;

  // Closed leg — realised P&L is locked in; no spot needed.
  if (!qty) return Number(p?.pnl || 0);

  const isCE  = sym.endsWith('CE');
  const isPE  = sym.endsWith('PE');
  const isFut = sym.endsWith('FUT') || (!isCE && !isPE && exch !== 'CDS');

  let v = null;
  if (isCE || isPE) {
    const decomp = decomposeSymbol(sym);
    const root   = (decomp.root || sym).toUpperCase();
    if (root) {
      const spot = _resolveSpot(p, root);
      if (spot > 0) {
        v = expiryPnl({ symbol: sym, qty, avg_cost: avg, kind: 'opt' }, spot);
      }
    }
  } else if (isFut) {
    // Futures: own LTP is the spot.
    const live = untrack(() => getSnapshot(sym)?.ltp) || Number(p?.last_price || 0);
    if (live > 0) {
      v = expiryPnl({ symbol: sym, qty, avg_cost: avg, kind: 'fut' }, live);
    }
  }

  if (v != null) return v + Number(p?.realised || 0);
  return null;
}

// ---------------------------------------------------------------------------
// Main reactive computation — runs at 4 Hz maximum.
// ---------------------------------------------------------------------------
const _computed = $derived.by(() => {
  // Register reactive dependency on the throttled tick.
  void _tick;

  const posRows  = positionsStore.value    ?? [];
  const holdRows = pulseHoldingsStore.value ?? [];

  let dayTotal    = 0;
  let expiryTotal = 0;
  /** @type {Record<string, number>} — keyed by uppercase tradingsymbol for setFromPulse compat */
  const byKey = {};
  /** @type {Record<string, { day: number, expiry: number }>} */
  const byRootPositions = {};
  /** @type {Record<string, { expiry: number }>} */
  const byRootHoldings  = {};
  /** @type {Map<string, number>} — per-account expiry P&L for NavBreakdown P slot */
  const expiryByAcct = new Map();

  // ---- Positions ----
  for (const p of posRows) {
    const sym = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
    if (!sym) continue;

    // Day P&L — uses untrack so per-symbol reads don't register extra deps.
    const snap    = untrack(() => getSnapshot(sym));
    const liveLtp = snap?.ltp ?? null;
    const dayVal  = livePositionDayPnl(
      {
        closePx: Number(p?.previous_close) || Number(p?.close_price ?? 0),
        pollLtp: Number(p?.last_price      ?? 0),
        qty:     Number(p?.quantity        ?? 0),
        avg:     Number(p?.average_price   ?? 0),
        dcvRow:  p,
      },
      liveLtp,
      { marketOpen: isMarketOpen() },
    );

    byKey[sym] = (byKey[sym] ?? 0) + dayVal;
    dayTotal  += dayVal;

    // Expiry P&L — F&O only; returns null for equity.
    const expVal = _expiryForPosition(p);
    if (expVal != null) {
      expiryTotal += expVal;

      // Per-account expiry accumulation (NavBreakdown P slot).
      const acct = String(p?.account || '');
      if (acct) expiryByAcct.set(acct, (expiryByAcct.get(acct) ?? 0) + expVal);

      const exch = String(p?.exchange || '').toUpperCase();
      if (FO_EXCHS.has(exch)) {
        const decomp = decomposeSymbol(sym);
        const root   = (decomp.root || sym).toUpperCase();
        if (root) {
          const slot = byRootPositions[root] ?? (byRootPositions[root] = { day: 0, expiry: 0 });
          slot.day    += dayVal;
          slot.expiry += expVal;
        }
      }
    }
  }

  // ---- Holdings expiry ----
  // Formula: (liveLtp − avgCost) × qty — expiry P&L (what we'd realise if we
  // sold at current price vs our average cost). This mirrors _accumulateHoldingExpPnl
  // in derivatives/+page.svelte which uses (spot − avg_cost) × qty.
  // NOT the day-P&L formula (liveLtp − closePx) × qty — that's a different concept.
  //
  // Proxy routing: if h.symbol is a proxy hedge (e.g. IDFIRSTB → CRUDEOIL),
  // targetsForProxy() returns the target root(s) to credit; otherwise credit the
  // holding's own tradingsymbol as root (equity symbols are their own root).
  for (const h of holdRows) {
    const sym = String(h?.tradingsymbol || h?.symbol || '').toUpperCase();
    if (!sym) continue;

    const snapH   = untrack(() => getSnapshot(sym));
    const snapLtp = snapH?.ltp;
    const liveLtp = (snapLtp != null && snapLtp > 0)
      ? Number(snapLtp)
      : Number(h?.last_price ?? 0);
    const avgCost = Number(h?.average_price ?? h?.avg_cost) || 0;
    const heldQty = Number(h?.quantity) || 0;

    if (!(liveLtp > 0) || heldQty === 0) continue;

    const expH = (liveLtp - avgCost) * heldQty;
    if (!isFinite(expH)) continue;

    // Proxy-hedge routing: targetsForProxy returns the underlying root(s) this
    // holding hedges. Falls back to [sym] (equity symbols are their own root).
    const targets = targetsForProxy(sym);
    const roots   = targets.length ? targets : [sym];
    for (const root of roots) {
      const slot = byRootHoldings[root] ?? (byRootHoldings[root] = { expiry: 0 });
      slot.expiry += expH;
    }
  }

  return { dayTotal, expiryTotal, byKey, byRootPositions, byRootHoldings, expiryByAcct };
});

/**
 * Unified reactive store for positions Day P&L + Expiry P&L.
 *
 * Accessors:
 *   .dayTotal        — aggregate live Day P&L (pulse-overridable)
 *   .total           — alias for .dayTotal (positionsDayPnlStore compat)
 *   .expiryTotal     — aggregate Exp P&L at current spot
 *   .byKey           — { [tradingsymbol]: dayPnl } (pulse-overridable)
 *   .expiryByAcct    — Map<account, expiryPnl> for NavBreakdown P slot
 *   .byRootPositions — { [root]: { day, expiry } } for Snapshot rows
 *   .byRootHoldings  — { [root]: { expiry } } for Snapshot Hold toggle
 *
 * setFromPulse(byKey, total) — called by MarketPulse after each buildUnified
 * with cq-accurate values. Takes priority over SSE-derived totals when set.
 */
export const positionsDerivedStore = {
  /** Aggregate live Day P&L — pulse-overridable. Alias for positionsDayPnlStore compat. */
  get total()           { return _pulseTotal ?? _computed.dayTotal; },
  /** Same as .total — preferred name in new code. */
  get dayTotal()        { return _pulseTotal ?? _computed.dayTotal; },
  get expiryTotal()     { return _computed.expiryTotal; },
  get byKey()           { return _pulseByKey ?? _computed.byKey; },
  get expiryByAcct()    { return _computed.expiryByAcct; },
  get byRootPositions() { return _computed.byRootPositions; },
  get byRootHoldings()  { return _computed.byRootHoldings; },

  /**
   * Called by MarketPulse after each buildUnified with cq-accurate per-symbol
   * and aggregate values. Takes priority over the SSE-only _computed derivation.
   * @param {Record<string,number>} byKey
   * @param {number} total
   */
  setFromPulse(byKey, total) {
    _pulseByKey = byKey;
    _pulseTotal = total;
  },
};
