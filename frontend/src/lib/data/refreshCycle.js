/**
 * refreshCycle — frontend wrapper around the backend's
 * /api/admin/persistence/mode/{off|soft|hard} endpoint.
 *
 * Backend modes (slice Z + BC):
 *   off  — normal three-tier read (memory → DB → broker)
 *   soft — bypass cache+DB, fetch from broker, heal on write-back
 *   hard — soft + ticker recycle (TickerManager.recycle())
 *
 * Frontend mirror (added in BH1):
 *   off  — no-op
 *   soft — drop in-memory of every market-data store + symbolStore;
 *          localStorage kept as stale-while-revalidate
 *   hard — drop in-memory AND localStorage + restart the SSE quote stream
 *          so the rebuilt backend ticker re-snapshots into a clean
 *          frontend store
 *
 * Every surface that flips persistence mode (today: /admin/health) goes
 * through `applyPersistenceMode()` so the cleanup is symmetric. Direct
 * calls to setPersistenceMode() would leave stale frontend caches around
 * even though the backend was reset.
 */

import { setPersistenceMode } from '$lib/api';
import {
  positionsStore, holdingsStore, fundsStore,
  watchQuotesStore, activeListsStore, moversStore, sparklinesStore,
} from './marketDataStores.svelte.js';
import { softReset as symbolSoftReset, hardReset as symbolHardReset } from './symbolStore.svelte.js';
import { restartQuoteStream, liveLtp } from './quoteStream.js';

/** @typedef {'off' | 'soft' | 'hard'} PersistenceMode */

/**
 * Every market-data store the cleanup paths iterate. Order is
 * deliberate: symbol-bearing stores first so any consumer reactive
 * to them sees the invalidation before the higher-level views
 * (funds, activeLists) update.
 */
const _STORES = [
  positionsStore,
  holdingsStore,
  watchQuotesStore,
  moversStore,
  sparklinesStore,
  fundsStore,
  activeListsStore,
];

/**
 * Flip the persistence mode. Calls the backend, then mirrors the
 * cleanup client-side on success. Failures leave both sides untouched
 * (the backend endpoint is transactional — either the mode change
 * landed or it didn't).
 *
 * @param {PersistenceMode} mode
 * @returns {Promise<{ mode: PersistenceMode, frontend_cleared: number }>}
 *   frontend_cleared: count of symbolStore entries dropped (diagnostic).
 */
export async function applyPersistenceMode(mode) {
  if (mode !== 'off' && mode !== 'soft' && mode !== 'hard') {
    throw new Error(`applyPersistenceMode: invalid mode ${mode}`);
  }
  // Backend first — if this fails, leave the frontend untouched so the
  // operator's existing data isn't dropped for no reason.
  await setPersistenceMode(mode);

  let cleared = 0;
  if (mode === 'soft') {
    cleared = symbolSoftReset();
    for (const s of _STORES) {
      try { s.softInvalidate?.(); } catch { /* no-op */ }
    }
    // liveLtp: drop in-memory; SSE will re-snapshot on its next tick.
    // Don't restart SSE here — soft mode preserves the ticker per the
    // backend contract.
    try { liveLtp.set({}); } catch { /* no-op */ }
  } else if (mode === 'hard') {
    cleared = symbolHardReset();
    for (const s of _STORES) {
      try { s.invalidate?.(); } catch { /* no-op */ }
    }
    try { liveLtp.set({}); } catch { /* no-op */ }
    // Backend recycled the ticker; restart SSE so the rebuilt stream
    // delivers a clean snapshot into the freshly-cleared store. Without
    // this the EventSource holds onto its connection to the now-dead
    // ticker session for up to 60s of native browser retry delay.
    try { restartQuoteStream(); } catch { /* no-op */ }
  }
  // mode === 'off' — backend resumed normal three-tier reads, no
  // frontend cleanup needed.

  return { mode, frontend_cleared: cleared };
}
