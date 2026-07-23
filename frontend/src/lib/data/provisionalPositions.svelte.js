/**
 * Provisional position rows — appear immediately after a `position_filled`
 * WS event, before the broker position API updates.
 *
 * Contract:
 *   applyFill(msg)   — called on `position_filled`; inserts/updates the row.
 *   clearFill(msg)   — called on `positions_refreshed`; drops the row once
 *                      the broker book has caught up.
 *   clearAll()       — safety reset (e.g. on logout / page unload).
 *   getProvisionalPositions() — reactive Map<key, row>; key =
 *                      `${exchange}:${tradingsymbol}:${account}`.
 *
 * The map is a Svelte 5 $state so any $derived that reads it will
 * recompute when applyFill / clearFill mutate state.
 */

/** @type {Map<string, any>} */
let _map = $state(new Map());

/** Returns the live reactive Map of provisional position rows. */
export function getProvisionalPositions() {
  return _map;
}

/**
 * Insert or update a provisional position row on fill.
 *
 * @param {{ account: string, exchange: string, tradingsymbol: string,
 *           qty: number, fill_price: number }} msg
 */
export function applyFill({ account, exchange, tradingsymbol, qty, fill_price }) {
  if (!tradingsymbol || typeof qty !== 'number') return;
  const key = `${exchange}:${tradingsymbol}:${account}`;
  const m = new Map(_map);
  const existing = m.get(key);
  if (existing) {
    // Additive — a second fill on the same symbol before the broker
    // book refreshes accumulates quantity.
    m.set(key, {
      ...existing,
      quantity:     (Number(existing.quantity) || 0) + qty,
      average_price: fill_price,   // rough proxy; broker will correct
      last_price:    fill_price,
    });
  } else {
    m.set(key, {
      account,
      exchange,
      tradingsymbol,
      quantity:      qty,
      average_price: fill_price,
      last_price:    fill_price,
      close_price:   0,
      pnl:           0,
      day_change_val: 0,
      realised:      0,
      mode:          'live',
      _provisional:  true,
    });
  }
  _map = m;
}

/**
 * Drop a provisional row once `positions_refreshed` confirms the broker
 * book reflects the fill.
 *
 * @param {{ exchange: string, tradingsymbol: string, account: string }} msg
 */
export function clearFill({ exchange, tradingsymbol, account }) {
  if (!tradingsymbol) return;
  const key = `${exchange}:${tradingsymbol}:${account}`;
  if (!_map.has(key)) return;
  const m = new Map(_map);
  m.delete(key);
  _map = m;
}

/** Drop ALL provisional rows (e.g. on logout or full positions reload). */
export function clearAll() {
  _map = new Map();
}
