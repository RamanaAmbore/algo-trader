/**
 * accountSort.js — canonical account display ordering for all UI surfaces.
 *
 * Operator-configured sequence (Jul 2026):
 *   Kite accounts (ZG0790, ZJ6294, …)  → display_order 10, 20, …
 *   Dhan DH3747 (primary Dhan)          → display_order 100
 *   Groww accounts                      → display_order 200, 210, …
 *   Other Dhan accounts                 → display_order 500
 *   Dhan DH6847 (last by request)       → display_order 999
 *
 * The order map is fetched once at boot from GET /api/admin/brokers/order
 * and stored in the `accountDisplayOrder` writable store. All UI surfaces
 * that list accounts call `sortAccountsBy(accounts, $accountDisplayOrder)`.
 *
 * To add a new UI surface:
 *   1. Import `accountDisplayOrder` from this module.
 *   2. Derive the sorted list: `sortAccountsBy(rawList, $accountDisplayOrder)`.
 *   3. Render from the sorted list. Do NOT sort inline by localeCompare.
 *
 * To change the order at runtime:
 *   PATCH /api/admin/brokers/{account} with { display_order: N }
 *   then call refreshAccountOrder() so the store updates without reload.
 */

import { fetchBrokerOrder } from '$lib/api';

/** @type {Record<string, number>} */
let _orderMap = {};

/** @type {Array<(map: Record<string,number>) => void>} */
let _listeners = [];

/** Cached order map (module-level, survives Svelte component re-mounts). */
export function getAccountOrderMap() {
  return _orderMap;
}

/**
 * Sort `accounts` by display_order (asc), then account_id (asc).
 * Unknown accounts (not in orderMap) are treated as display_order=999.
 *
 * @param {string[]} accounts
 * @param {Record<string, number>} orderMap
 * @returns {string[]}
 */
export function sortAccountsBy(accounts, orderMap) {
  const map = orderMap || _orderMap;
  return [...accounts].sort((a, b) => {
    const oa = map[a] ?? 999;
    const ob = map[b] ?? 999;
    if (oa !== ob) return oa - ob;
    return a.localeCompare(b);
  });
}

/**
 * Subscribe to order-map updates. Returns an unsubscribe function.
 * Compatible with Svelte's `$store` auto-subscribe protocol.
 *
 * @param {(map: Record<string,number>) => void} fn
 * @returns {() => void}
 */
function subscribe(fn) {
  _listeners.push(fn);
  fn(_orderMap);   // immediate call with current value
  return () => {
    _listeners = _listeners.filter(l => l !== fn);
  };
}

function _notify() {
  for (const fn of _listeners) fn(_orderMap);
}

/**
 * Writable-store–compatible object for `accountDisplayOrder`.
 * Svelte components can use `$accountDisplayOrder` once imported.
 */
export const accountDisplayOrder = {
  subscribe,
  /** Call after PATCH display_order to force a re-fetch. */
  refresh: loadAccountOrder,
};

/**
 * Fetch the order map from the API and update the store.
 * Called once at app boot from (algo)/+layout.svelte.
 * Safe to call multiple times — deduplicates in-flight requests.
 */
let _loadingPromise = /** @type {Promise<void> | null} */ (null);

export async function loadAccountOrder() {
  if (_loadingPromise) return _loadingPromise;
  _loadingPromise = (async () => {
    try {
      const map = await fetchBrokerOrder();
      if (map && typeof map === 'object') {
        _orderMap = map;
        _notify();
      }
    } catch (_) {
      // Non-fatal: UI falls back to insertion order.
    } finally {
      _loadingPromise = null;
    }
  })();
  return _loadingPromise;
}
