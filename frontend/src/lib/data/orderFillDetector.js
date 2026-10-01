/**
 * orderFillDetector.js — channel-agnostic "order log just showed Filled"
 * trigger for a forced-fresh books refresh.
 *
 * Why this exists (2026-09-30 follow-up to commit c90a9d04): that commit
 * fixed the LAST link in the chain — the derivatives page's legs-watcher
 * effect now re-triggers `loadStrategy()` whenever the legs signature
 * changes, not only on an underlying switch. But the FIRST link — "order
 * log shows FILLED → immediate fresh books fetch" — only ever fired for
 * WS-pushed events (`position_filled`, `positions_refreshed`,
 * `order_update`/`book_changed`). Those WS events are broadcast exclusively
 * by `_postback_broadcast_fanout` (backend/api/routes/orders.py), which
 * only runs on a genuine broker POSTBACK. Three other channels that can
 * flip an order's DISPLAYED status to FILLED/COMPLETE never broadcast
 * anything at all:
 *
 *   1. The live broker-order-book read (`fetchOrders()` → `/api/orders/`,
 *      a 15s-TTL passthrough of `broker.orders()`) — a fill can show up
 *      here the moment the broker's own book says COMPLETE, with zero
 *      backend state mutation.
 *   2. The 5-min `_task_open_order_watchdog` sweep (backend/api/
 *      background.py) — the documented Dhan/Groww backstop for when a
 *      webhook isn't configured/delivered. Flips AlgoOrder.status to
 *      FILLED directly but never invalidates positions/holdings caches
 *      nor broadcasts any WS event.
 *   3. The admin `/algo/reconcile` sweep and the per-card `/{id}/reconcile`
 *      button — same gap as #2, operator-triggered instead of scheduled.
 *
 * Rather than patching N backend reconcile call sites (and re-deriving
 * broker id / lots-vs-contracts conversion correctly for each), this
 * module hooks the one place ALL of the above ultimately surface: the
 * merged row array `OrderBook.svelte` / `LogPanel.svelte` already build
 * on every poll (same idiom as `templateAttachToast.js`'s
 * `noteAttachObservation`, which already runs on this exact merged array
 * for a different purpose). The moment any row's status transitions INTO
 * a filled state, bump the shared `bookChanged` counter — the SAME bus
 * every WS fill handler already bumps (see `bookChanged.js`), so every
 * existing subscriber (derivatives Payoff/Legs, MarketPulse,
 * PerformancePage, dashboard hero, /orders page) force-refreshes via
 * their own existing `loadPositions({ fresh: true })` / `loadStrategy()`
 * calls — no second refresh mechanism invented.
 *
 * Module-level Map, shared by every caller in the tab (OrderBook and
 * LogPanel observing the same transition fire the bus only once).
 */

import { bookChanged } from './bookChanged.js';

const _FILLED_STATUSES = new Set(['COMPLETE', 'FILLED']);

function _isFilled(status) {
  return _FILLED_STATUSES.has(String(status || '').toUpperCase());
}

/** @type {Map<string, string>} */
const _lastStatus = new Map();

/** First-ever call across the tab session only seeds the map — orders
 *  that are ALREADY filled at page-load time are not "new" fills and
 *  must not bump the bus (every page would force-refresh on mount). */
let _seeded = false;

/**
 * Call once per poll with the FULL merged order-row array (broker rows +
 * algo-only rows, same shape `_loadOrders()` already builds BEFORE any
 * status-chip filtering — mirrors `noteAttachObservation`'s contract).
 *
 * Fires (bumps `bookChanged` once, coalescing a whole poll/basket burst
 * into one refresh) when, after the initial seeding pass:
 *   - a previously-seen order_id moves from a non-filled to a filled
 *     status, or
 *   - an order_id never seen before turns up already filled (a MARKET
 *     order can complete inside one poll's TTL window and never be
 *     observed OPEN first — diffing only known keys would miss this,
 *     the single most common fill shape).
 *
 * An order_id that was already filled on a prior call and stays filled
 * does not refire — this is a one-shot edge trigger, not a level signal.
 *
 * @param {any[]} rows
 */
export function noteOrderPollFills(rows) {
  if (!Array.isArray(rows) || rows.length === 0) {
    _seeded = true;
    return;
  }
  const firstCall = !_seeded;
  _seeded = true;
  let fired = false;
  for (const o of rows) {
    const id = o?.order_id ?? o?.id;
    if (id == null) continue;
    const key = String(id);
    const status = String(o?.status || '').toUpperCase();
    const prev = _lastStatus.get(key);
    _lastStatus.set(key, status);
    if (firstCall) continue; // seeding pass — never fires
    if (_isFilled(status) && !(prev !== undefined && _isFilled(prev))) {
      fired = true;
    }
  }
  if (fired) bookChanged.update(n => n + 1);
}

/** Test-only reset — clears seeding + per-order state. */
export function _resetOrderFillDetector() {
  _lastStatus.clear();
  _seeded = false;
}
