/**
 * underlyingSpotStore — shared SSOT for underlying spot quotes fetched via
 * batchQuote. Both PositionStrip (NavStrip Exp P&L) and the derivatives page
 * write through this store so they share the same spot price for MCX futures
 * and index underlyings. Eliminates the NavStrip/derivatives Exp P&L divergence
 * caused by PositionStrip falling back to symbolStore (which only holds option
 * LTPs, not futures LTPs).
 *
 * Pattern: module-local $state, exported only via getter (same shape as
 * positionsDayPnlStore). Svelte 5 forbids exporting a reassigned $state
 * binding directly — wrap in an object with a get accessor instead.
 *
 * No polling logic here. Callers decide when to call loadUnderlyingSpots().
 */

import { batchQuote } from '$lib/api.js';
import { publishPulseQuotes } from '$lib/data/marketDataStores.svelte.js';
import { applyUnderlyingTickLtp } from '$lib/data/underlyingQuoteUtils.js';

/**
 * Module-local reactive map: { ROOT: { ltp, day_pct, prev_close } }
 * e.g. { CRUDEOIL: { ltp: 5788, day_pct: 0.4, prev_close: 5763 } }
 * @type {Record<string, { ltp: number, day_pct: number | null, prev_close: number }>}
 */
let _quotes = $state({});

/**
 * Returns the underlying spot LTP for the given root.
 * Falls back to 0 when root is not yet loaded.
 *
 * @param {string} root - e.g. "CRUDEOIL", "NIFTY", "GOLD"
 * @returns {number}
 */
export function getUnderlyingSpot(root) {
  return _quotes[root]?.ltp ?? 0;
}

/**
 * Read-only handle to the reactive quote map. Use `.value` to access the
 * current snapshot — mirrors the dataStore pattern used elsewhere.
 */
export const underlyingSpotStore = {
  get value() { return _quotes; },
};

/**
 * Fetch batchQuote for the provided (root, quoteKey) pairs, populate the
 * shared store, and publish to symbolStore via publishPulseQuotes so live
 * option payoff charts and other symbolStore consumers stay in sync.
 *
 * @param {Array<{ root: string, quoteKey: string }>} pairs
 * @returns {Promise<void>}
 */
export async function loadUnderlyingSpots(pairs) {
  if (!pairs || pairs.length === 0) return;

  const keys = pairs.map(p => p.quoteKey);
  const res = await batchQuote(keys);
  const items = res?.items ?? [];

  // Publish to symbolStore so liveSpot / OptionsPayoff consumers receive the
  // latest anchors without a separate batchQuote call.
  publishPulseQuotes(items);

  // Build exchange:symbol → item map.
  /** @type {Record<string, any>} */
  const byKey = {};
  for (const it of items) {
    if (!it?.exchange || !it?.tradingsymbol) continue;
    byKey[`${it.exchange}:${it.tradingsymbol}`] = it;
  }

  /** @type {Record<string, { ltp: number, day_pct: number | null, prev_close: number }>} */
  const next = {};
  for (const { root, quoteKey } of pairs) {
    const q = byKey[quoteKey];
    if (!q) continue;
    const ltp   = Number(q.ltp   ?? q.last_price ?? 0);
    const close = Number(q.close ?? q.ohlc?.close ?? 0);
    let pct = null;
    if (q.change_pct != null)          pct = Number(q.change_pct);
    else if (q.change_percent != null) pct = Number(q.change_percent);
    else if (close > 0 && ltp > 0)    pct = ((ltp - close) / close) * 100;
    next[root] = { ltp, day_pct: pct, prev_close: close };
  }
  _quotes = { ..._quotes, ...next };
}

/**
 * Apply a single live-tick LTP patch to the store without triggering a full
 * batchQuote reload. Preserves day_pct and prev_close from the last batchQuote.
 * Used by tickBus handlers in the derivatives page so NavStrip (via getUnderlyingSpot)
 * also sees per-tick updates, not just 30s poll refreshes.
 *
 * No-op when root is not yet loaded (prevents phantom entries before first load).
 *
 * @param {string} root - e.g. "CRUDEOIL", "NIFTY"
 * @param {number | null | undefined} ltp
 */
export function patchUnderlyingSpot(root, ltp) {
  const next = applyUnderlyingTickLtp(_quotes, root, ltp);
  if (next !== _quotes) _quotes = next;
}
