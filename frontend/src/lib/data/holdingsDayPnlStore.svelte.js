/**
 * holdingsDayPnlStore — module-level singleton that aggregates live
 * day P&L across all holdings at 4 Hz (250ms throttle).
 *
 * Reads `pulseHoldingsStore.value` — same source as PositionStrip H slot display,
 * ensuring day P&L total and value/lifetime columns reflect the same fetch.
 * Throttles at 4 Hz via `symbolTickCount.subscribe` + 250ms setTimeout
 * gate — same pattern as positionsDayPnlStore.
 *
 * Formula per row (mirrors mergeHoldingRows and _liveHoldingsToday):
 *   liveLtp  = getSnapshot(sym)?.ltp ?? h.last_price
 *   closePx  = h.previous_close || h.close_price || h.ohlc?.close || 0
 *   avgCost  = Number(h.average_price) || 0
 *   heldQty  = Number(h.quantity) || 0
 *   Guard: if closePx===0 or closePx===avgCost → fall back to day_change_val
 *   day_pnl  = (liveLtp > 0 && heldQty !== 0 && |liveLtp−closePx| > 0.005)
 *              ? (liveLtp − closePx) × heldQty
 *              : Number(h.day_change_val) || 0
 *
 * Exports:
 *   { total: number, byKey: { [tradingsymbol: string]: number }, byAccount: { [account: string]: number } }
 *
 * `byKey` keys are plain uppercase tradingsymbols (no exchange prefix).
 * `byAccount` is keyed by uppercase account ID; 'TOTAL' key holds the grand total.
 *
 * Pulse override: MarketPulse calls setFromPulse() after each buildUnified pass
 * so that total/byKey reflect the cq-accurate per-symbol values from the grid.
 * byAccount is always from _store (all-accounts, not overridden by pulse).
 */

import { browser } from '$app/environment';
import { untrack } from 'svelte';
import { symbolTickCount, getSnapshot } from '$lib/data/symbolStore.svelte.js';
import { pulseHoldingsStore } from '$lib/data/marketDataStores.svelte.js';

// Module-level throttle state — safe: one instance per bundled app.
let _tick = $state(0);
/** @type {ReturnType<typeof setTimeout> | null} */
let _tickTimer = null;

// Pulse override state — set by MarketPulse.setFromPulse() after each
// buildUnified pass. Mirrors positionsDayPnlStore's _pulseTotal/_pulseByKey.
// null means no pulse override yet; store falls back to _store values.
let _pulseTotal = $state(/** @type {number|null} */ (null));
let _pulseByKey = $state(/** @type {Record<string,number>|null} */ (null));

// Throttle at 4 Hz (250 ms) — mirrors positionsDayPnlStore pattern.
// Wrapped in browser guard: SSR has no setTimeout and no symbolTickCount stream.
if (browser) {
  symbolTickCount.subscribe(() => {
    if (_tickTimer) return;
    _tickTimer = setTimeout(() => {
      _tickTimer = null;
      _tick++;
    }, 250);
  });
}

const _store = $derived.by(() => {
  // Register reactive dependency on the throttled tick so this block
  // re-runs at most 4 times/sec during SSE burst.
  void _tick;

  const rows = pulseHoldingsStore.value ?? [];
  let total = 0;
  /** @type {Record<string, number>} */
  const byKey = {};
  /** @type {Record<string, number>} */
  const byAccount = {};

  for (const h of rows) {
    const sym = String(h?.tradingsymbol || h?.symbol || '').toUpperCase();
    if (!sym) continue;

    // Use untrack() so individual symbol reads don't register as
    // per-symbol reactive deps — the throttled tick drives recompute.
    const snap    = untrack(() => getSnapshot(sym));
    const snapLtp = snap?.ltp;

    // Prefer snapshot LTP; fall back to broker last_price for symbols
    // not subscribed on the ticker (equity holdings off watchlist).
    const liveLtp = (snapLtp != null && snapLtp > 0)
      ? Number(snapLtp)
      : Number(h?.last_price ?? 0);

    const closePx  = Number(h?.previous_close) || Number(h?.close_price) || Number(h?.ohlc?.close) || 0;
    const avgCost  = Number(h?.average_price) || 0;
    const heldQty  = Number(h?.quantity) || 0;
    const dcv      = Number(h?.day_change_val)  || 0;

    let val;
    // Guard: if closePx is 0 or equals avgCost, the close field is absent or
    // mixed with average_price (a data-source bug). Fall back to dcv to
    // avoid computing a wrong value (lifetime P&L instead of day P&L).
    if (closePx === 0 || closePx === avgCost) {
      if (import.meta.env.DEV && closePx === avgCost && avgCost > 0) {
        console.warn('[holdingsDayPnlStore] closePx === avgCost for', sym, '— falling back to day_change_val');
      }
      val = dcv;
    } else if (liveLtp > 0 && heldQty !== 0 && Math.abs(liveLtp - closePx) > 0.005) {
      // Live formula — mirrors _liveHoldingsToday and mergeHoldingRows.
      // Post-settlement guard: skip when ltp ≈ close (Kite resets
      // last_price = close_price = settlement_price → delta ≈ 0).
      val = (liveLtp - closePx) * heldQty;
    } else {
      // Fallback: broker's day_change_val (no new-position split needed
      // for holdings — they never have an overnight_quantity=0 edge case).
      val = dcv;
    }

    byKey[sym] = (byKey[sym] ?? 0) + val;
    total += val;

    // Accumulate per-account totals for holdingsSummaryData filter path.
    const acc = String(h?.account || '').toUpperCase();
    if (acc) byAccount[acc] = (byAccount[acc] ?? 0) + val;
  }

  byAccount['TOTAL'] = total;

  return { total, byKey, byAccount };
});

/**
 * Singleton store for holdings day P&L.
 *
 * - total / byKey: pulse-overridable (MarketPulse calls setFromPulse after
 *   each buildUnified so NavStrip H reads the same value the grid displays,
 *   and is filter-aware like NavStrip P).
 * - byAccount: always from _store (all-accounts; never overridden by pulse).
 */
export const holdingsDayPnlStore = {
  get total()     { return _pulseTotal ?? _store.total; },
  get byKey()     { return _pulseByKey ?? _store.byKey; },
  /** Always all-accounts; not overridden by setFromPulse. */
  get byAccount() { return _store.byAccount; },
  /**
   * Called by MarketPulse after each buildUnified with cq-accurate per-symbol
   * and aggregate values. Mirrors positionsDayPnlStore.setFromPulse.
   * @param {Record<string,number>} byKey
   * @param {number} total
   */
  setFromPulse(byKey, total) {
    _pulseByKey  = byKey;
    _pulseTotal  = total;
  },
};
