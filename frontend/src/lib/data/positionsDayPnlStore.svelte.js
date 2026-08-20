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
 * @type {{ readonly total: number, readonly byKey: Record<string, number> }}
 */
export const positionsDayPnlStore = {
  get total() { return _store.total; },
  get byKey()  { return _store.byKey;  },
};
