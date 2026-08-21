/**
 * positionsDayPnlStore — module-level singleton that aggregates live
 * day P&L across all positions at 4 Hz (250ms throttle).
 *
 * Reads `positionsStore.value` (kept fresh by the 5s book poller).
 * Throttles at 4 Hz via `symbolTickCount.subscribe` + 250ms setTimeout
 * gate — same pattern as PositionStrip._throttledTick.
 *
 * Exports:
 *   { total: number, byKey: { [sym: string]: number } }
 *
 * `byKey` keys are uppercase tradingsymbols (no exchange prefix) so they
 * map directly onto MarketPulse's `byKey` via `${sym}__pos`.
 */

import { browser } from '$app/environment';
import { untrack } from 'svelte';
import { symbolTickCount, getSnapshot } from '$lib/data/symbolStore.svelte.js';
import { positionsStore } from '$lib/data/marketDataStores.svelte.js';
import { livePositionDayPnl } from '$lib/data/nav.js';
import { isMarketOpen } from '$lib/marketHours';

// Module-level throttle state — safe because this module is never
// instantiated more than once (Vite keeps one instance per bundled app).
let _tick = $state(0);
/** @type {ReturnType<typeof setTimeout> | null} */
let _tickTimer = null;

// Throttle at 4 Hz (250 ms) — same as PositionStrip._throttledTick.
// Wrap in `browser` guard: SSR has no setTimeout and no symbolTickCount
// stream, so the subscribe would throw in Node.
if (browser) {
  symbolTickCount.subscribe(() => {
    if (_tickTimer) return;
    _tickTimer = setTimeout(() => {
      _tickTimer = null;
      _tick++;
    }, 250);
  });
}

// Pulse-authoritative override — written by MarketPulse after each buildUnified.
// Takes priority over the SSE-only _store computation so NavStrip P reads the
// same cq-accurate value that Pulse displays per row.
let _pulseTotal = $state(/** @type {number|null} */ (null));
let _pulseByKey = $state(/** @type {Record<string,number>|null} */ (null));

const _store = $derived.by(() => {
  // Register reactive dependency on the throttled tick so this block
  // re-runs at most 4 times/sec during SSE burst.
  void _tick;

  const rows = positionsStore.value ?? [];
  let total = 0;
  /** @type {Record<string, number>} */
  const byKey = {};

  for (const r of rows) {
    const sym = String(r?.tradingsymbol || r?.symbol || '').toUpperCase();
    if (!sym) continue;

    // Use untrack() so individual symbol reads don't register as
    // per-symbol reactive deps — the throttled tick drives recompute.
    const snap    = untrack(() => getSnapshot(sym));
    const liveLtp = snap?.ltp ?? null;

    const val = livePositionDayPnl(
      {
        closePx: Number(r.previous_close) || Number(r.close_price ?? 0),
        pollLtp: Number(r.last_price    ?? 0),
        qty:     Number(r.quantity      ?? 0),
        avg:     Number(r.average_price ?? 0),
        dcvRow:  r,
      },
      liveLtp,
      { marketOpen: isMarketOpen() },
    );

    byKey[sym] = (byKey[sym] ?? 0) + val;
    total += val;
  }

  return { total, byKey };
});

/**
 * Singleton store for positions day P&L.
 *
 * `total` and `byKey` are sourced from Pulse (_pulseTotal / _pulseByKey) when
 * available — Pulse computes these using live cq quotes and is more accurate
 * than the SSE-throttled _store derivation. Falls back to _store when Pulse
 * has not yet written (e.g. on pages that don't mount MarketPulse).
 */
export const positionsDayPnlStore = {
  get total() { return _pulseTotal ?? _store.total; },
  get byKey()  { return _pulseByKey ?? _store.byKey;  },
  /**
   * Called by MarketPulse after each buildUnified with cq-accurate per-symbol
   * and aggregate values. Takes priority over the SSE-only _store computation.
   * @param {Record<string,number>} byKey
   * @param {number} total
   */
  setFromPulse(byKey, total) {
    _pulseByKey = byKey;
    _pulseTotal = total;
  },
};
