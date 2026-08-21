/**
 * mergePositionStores.js — pure helper for positionsDayPnlStore.
 *
 * Merges two position-row arrays (from `positionsStore` and
 * `pulsePositionsStore`) into one deduplicated array, keyed by
 * `tradingsymbol|symbol : account`.
 *
 * Preference rule: when the same key appears in both arrays, the row with a
 * non-zero `day_change_val` is preferred over a zero-dcv row. This handles
 * the first-mount race where the cross-page 5s poller (`positionsStore`) may
 * still be empty while Pulse's `loadPulse()` has already populated
 * `pulsePositionsStore` with fresh rows.
 *
 * Exported as a standalone module so it can be unit-tested without pulling in
 * any SvelteKit virtual modules (`$app/environment`, etc.).
 */

/**
 * @param {Array<object>} p1 - Rows from positionsStore.value
 * @param {Array<object>} p2 - Rows from pulsePositionsStore.value
 * @returns {Array<object>} Merged rows (one per symbol+account key)
 */
export function mergePositionStores(p1, p2) {
  /** @type {Map<string, object>} */
  const bySymAcct = new Map();
  for (const r of [...p1, ...p2]) {
    const k = `${(r.tradingsymbol || r.symbol || '')}:${r.account || ''}`;
    const existing = bySymAcct.get(k);
    if (!existing) {
      bySymAcct.set(k, r);
    } else if (Number(r.day_change_val) !== 0 && Number(existing.day_change_val) === 0) {
      bySymAcct.set(k, r);
    }
  }
  return [...bySymAcct.values()];
}
