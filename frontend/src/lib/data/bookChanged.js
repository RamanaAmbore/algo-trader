/**
 * bookChanged — singleton subscriber for the `book_changed` WebSocket
 * event emitted by the broker postback handler on every terminal
 * order status (COMPLETE / CANCELLED / REJECTED / EXPIRED).
 *
 * Why centralize: previously each surface that displays position-
 * derived data (snapshot grid, legs panel, payoff curve, dashboard
 * hero, performance grid) had to poll independently for fresh
 * positions. Postback invalidated `orders` cache but NOT
 * `positions`/`holdings`, so the snapshot grid took 2+ iterations
 * to settle — first poll showed patched per-cell qty (via the
 * `position_filled` event), second poll picked up the recomputed
 * aggregates from the strategy endpoint.
 *
 * Now: backend emits ONE `book_changed` event on every terminal
 * status. This module subscribes once via `createPerformanceSocket`
 * and re-broadcasts to every page via the `bookChanged` store.
 * Pages call their primary loader on store change (debounced 200ms
 * so a burst of fills coalesces into one refresh).
 *
 * Usage in a page:
 *
 *   import { bookChanged } from '$lib/data/bookChanged';
 *   $effect(() => {
 *     if ($bookChanged > 0) {
 *       loadPositions();
 *       loadStrategy();
 *     }
 *   });
 *
 * The store value is a monotonically-increasing counter (not the
 * event payload) so the effect re-runs on every increment. Pages
 * that need the changed (account, symbol, exchange) tuple can read
 * `lastBookEvent` separately.
 */

import { writable } from 'svelte/store';
import { createPerformanceSocket } from '$lib/ws';

/** Monotonic counter — increments on every book_changed event.
 *  Effects depending on this store re-run automatically. */
export const bookChanged = writable(0);

/** Latest event payload — null until first event. Pages that need
 *  scope info (account / symbol / exchange) can branch on this. */
export const lastBookEvent = writable(/** @type {null|{account:string, exchange:string, tradingsymbol:string, reason:string, ts:number}} */ (null));

let _unsub = null;
let _started = false;
/** Coalesce bursts — multiple postbacks landing within 200ms
 *  produce a single store increment. Keeps downstream loaders from
 *  firing N times for a basket-order fill. */
let _debounceTimer = null;
let _pendingEvent = null;

/** Idempotent — call from app root or any page. Multiple calls
 *  share the same WS subscription. */
export function startBookChangedBus() {
  if (_started) return;
  _started = true;
  try {
    _unsub = createPerformanceSocket((msg) => {
      if (!msg || msg.event !== 'book_changed') return;
      _pendingEvent = msg;
      clearTimeout(_debounceTimer);
      _debounceTimer = setTimeout(() => {
        if (_pendingEvent) {
          lastBookEvent.set(_pendingEvent);
          _pendingEvent = null;
        }
        bookChanged.update(n => n + 1);
      }, 200);
    });
  } catch (e) {
    // Never let WS plumbing break a page mount. The store stays at
    // 0 and pages fall back to their existing pollers.
    // eslint-disable-next-line no-console
    console.warn('bookChanged bus startup failed:', e);
    _started = false;
  }
}

/** Tear down the bus. Tests + dev hot-reload may want this; pages
 *  don't (the bus is process-scoped, not page-scoped). */
export function stopBookChangedBus() {
  _started = false;
  clearTimeout(_debounceTimer);
  _debounceTimer = null;
  _pendingEvent = null;
  if (_unsub) {
    try { _unsub(); } catch {}
    _unsub = null;
  }
}
