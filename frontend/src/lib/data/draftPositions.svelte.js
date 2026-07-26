/**
 * Draft position rows — operator-confirmed hypothetical positions stored
 * in module state (not tied to a local drafts[] input array).
 *
 * These are "D"-marked legs that appear in the derivatives legs display
 * below provisional (~) rows and above nothing. Distinct from:
 *   - local `drafts` input array (operator types a symbol/qty into form inputs)
 *   - provisionalPositions (post-fill, pre-broker-refresh rows)
 *
 * Contract:
 *   addDraftPosition(msg)       — insert or update a draft store row.
 *   removeDraftPosition(msg)    — drop by (exchange, tradingsymbol, account).
 *   clearAllDraftPositions()    — safety reset (e.g. on logout / page unload).
 *   getDraftPositions()         — reactive Map<key, row>; key =
 *                                 `${exchange}:${tradingsymbol}:${account}`.
 *
 * The map is a Svelte 5 $state so any $derived that reads it will
 * recompute when addDraftPosition / removeDraftPosition mutate state.
 */

/** @type {Map<string, any>} */
let _map = $state(new Map());

/** Returns the live reactive Map of draft position rows. */
export function getDraftPositions() {
  return _map;
}

/**
 * Insert or update a draft position row.
 *
 * @param {{ account: string, exchange: string, tradingsymbol: string,
 *           qty: number, fill_price: number }} msg
 */
export function addDraftPosition({ account, exchange, tradingsymbol, qty, fill_price }) {
  if (!tradingsymbol || typeof qty !== 'number') return;
  const key = `${exchange}:${tradingsymbol}:${account}`;
  const m = new Map(_map);
  const existing = m.get(key);
  if (existing) {
    m.set(key, {
      ...existing,
      quantity:      (Number(existing.quantity) || 0) + qty,
      average_price: fill_price,
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
      _draft_store:  true,
    });
  }
  _map = m;
}

/**
 * Drop a draft store row by key.
 *
 * @param {{ exchange: string, tradingsymbol: string, account: string }} msg
 */
export function removeDraftPosition({ exchange, tradingsymbol, account }) {
  if (!tradingsymbol) return;
  const key = `${exchange}:${tradingsymbol}:${account}`;
  if (!_map.has(key)) return;
  const m = new Map(_map);
  m.delete(key);
  _map = m;
}

/** Drop ALL draft store rows (e.g. on logout or full reset). */
export function clearAllDraftPositions() {
  _map = new Map();
}
