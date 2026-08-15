/**
 * holdingsDayPnlStore — module-level singleton that aggregates live
 * day P&L across all holdings at 4 Hz (250ms throttle).
 *
 * Reads `holdingsStore.value` (kept fresh by the cross-page book poller).
 * Throttles at 4 Hz via `symbolTickCount.subscribe` + 250ms setTimeout
 * gate — same pattern as positionsDayPnlStore.
 *
 * Formula per row (mirrors mergeHoldingRows and _liveHoldingsToday):
 *   liveLtp  = getSnapshot(sym)?.ltp ?? h.last_price
 *   closePx  = Number(h.close_price) || 0
 *   heldQty  = Number(h.opening_quantity) || Number(h.quantity) || 0
 *   day_pnl  = (liveLtp > 0 && closePx > 0 && heldQty !== 0 && |liveLtp−closePx| > 0.005)
 *              ? (liveLtp − closePx) × heldQty
 *              : Number(h.day_change_val) || 0
 *
 * Exports:
 *   { total: number, byKey: { [tradingsymbol: string]: number } }
 *
 * `byKey` keys are plain uppercase tradingsymbols (no exchange prefix) so
 * MarketPulse can look them up via `byKey[sym]` and override row.day_pnl
 * for `${sym}__hold` rows.
 */

import { browser } from '$app/environment';
import { untrack } from 'svelte';
import { symbolTickCount, getSnapshot } from '$lib/data/symbolStore.svelte.js';
import { holdingsStore } from '$lib/data/marketDataStores.svelte.js';

// Module-level throttle state — safe: one instance per bundled app.
let _tick = $state(0);
/** @type {ReturnType<typeof setTimeout> | null} */
let _tickTimer = null;

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

  const rows = holdingsStore.value ?? [];
  let total = 0;
  /** @type {Record<string, number>} */
  const byKey = {};

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

    const closePx  = Number(h?.close_price)    || 0;
    const heldQty  = Number(h?.opening_quantity) || Number(h?.quantity) || 0;
    const dcv      = Number(h?.day_change_val)  || 0;

    let val;
    if (liveLtp > 0 && closePx > 0 && heldQty !== 0 && Math.abs(liveLtp - closePx) > 0.005) {
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
  }

  return { total, byKey };
});

/**
 * Singleton store for holdings day P&L.
 *
 * @type {{ readonly total: number, readonly byKey: Record<string, number> }}
 */
export const holdingsDayPnlStore = {
  get total() { return _store.total; },
  get byKey()  { return _store.byKey;  },
};
