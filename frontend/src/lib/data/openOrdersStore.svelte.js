import { fetchOrderEvents } from '$lib/api';
import { writable } from 'svelte/store';

/** symbol → total pending qty across all OPEN orders */
export const openOrderQtyBySymbol = writable(/** @type {Record<string,number>} */ ({}));

export async function pollOpenOrders() {
  try {
    const res = await fetchOrderEvents(200, 'open');
    const raw = Array.isArray(res) ? res : (res?.events ?? []);
    /** @type {Record<string,number>} */
    const map = {};
    for (const ev of raw) {
      const sym = ev.symbol ?? ev.tradingsymbol;
      if (!sym) continue;
      const qty = Number(ev.quantity || 0);
      if (qty > 0) map[sym] = (map[sym] || 0) + qty;
    }
    openOrderQtyBySymbol.set(map);
  } catch (_) { /* keep stale on error */ }
}
