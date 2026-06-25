/**
 * marketDataStores — module-level singletons for the three most-used
 * data classes (positions / holdings / funds). Every consumer imports
 * the same instance, so a load() triggered in PositionStrip is
 * immediately visible in the navbar, dashboard, etc. without any
 * parent-to-child prop threading.
 *
 * Migration status:
 *   PositionStrip — migrated (slice Z)
 *   MarketPulse   — pending (next slice; still uses its own cachedRead)
 *   /admin/dashboard — pending
 *   /orders          — pending
 *
 * The `parse` selectors mirror what each ad-hoc caller was doing inline:
 *   positions/holdings — extract `.rows`, default to []
 *   funds              — extract `.rows`, strip the TOTAL aggregate row
 *                        so callers can sum across accounts without
 *                        double-counting. The TOTAL row is still used
 *                        on the /performance page — it reads raw API
 *                        directly and is not affected here.
 */

import { createDataStore, TTL } from './dataStore.svelte.js';
import { fetchPositions, fetchHoldings, fetchFunds } from '$lib/api';

export { TTL };

/** @typedef {import('./dataStore.svelte.js').createDataStore} DS */

/**
 * Live positions (open + intraday-closed).
 * Keyed to `md.positions` in localStorage (TTL 15 min; aggressively
 * refreshed on mount so the cache is only for instant-paint).
 */
export const positionsStore = createDataStore({
  key:     'md.positions',
  fetcher: fetchPositions,
  ttl:     TTL.minute,
  /** @param {any} r */
  parse:   (r) => r?.rows ?? [],
});

/**
 * Holdings (overnight / long-term book).
 * TTL 15 min — same reasoning as positions.
 */
export const holdingsStore = createDataStore({
  key:     'md.holdings',
  fetcher: fetchHoldings,
  ttl:     TTL.minute,
  /** @param {any} r */
  parse:   (r) => r?.rows ?? [],
});

/**
 * Margin / funds snapshot. The /api/funds response includes a synthetic
 * TOTAL aggregate row so the performance page can display it without
 * an extra summation step. Consumers here (PositionStrip, NavCard) sum
 * per-account rows themselves — strip TOTAL to avoid double-counting.
 */
export const fundsStore = createDataStore({
  key:     'md.funds',
  fetcher: fetchFunds,
  ttl:     TTL.minute,
  /** @param {any} r */
  parse:   (r) => (r?.rows ?? []).filter(
    (/** @type {any} */ x) => x && x.account && x.account !== 'TOTAL'
  ),
});
