/**
 * gttMatch.js — pair stored order GTT legs with the broker's live GTT rows.
 *
 * An order keeps its attached GTTs in `attached_gtts_json` (entries with
 * kind 'gtt', a label, and the broker GTT id). The broker's list (fetchGtts)
 * carries each GTT's live status. Matching is by GTT id.
 */

/** Parse `attached_gtts_json` into an array of entries (empty when unset or invalid). */
export function parseAttached(json) {
  if (!json) return [];
  try {
    const parsed = typeof json === 'string' ? JSON.parse(json) : json;
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

/** Human status for a broker GTT status string. */
export function gttStatusLabel(status) {
  const s = String(status || '').toLowerCase();
  if (s === 'active') return 'active';
  if (s === 'triggered') return 'triggered';
  if (s === 'disabled' || s === 'cancelled') return 'cancelled';
  if (s === 'expired') return 'expired';
  return s || 'unknown';
}

/**
 * Match every order's GTT legs to broker rows.
 *
 * @param {Array<{order_id?: any, id?: any, attached_gtts_json?: any}>} orders
 * @param {Array<{gtt_id: string, status: string, trigger_values?: number[], tradingsymbol?: string, account?: string, created_at?: string}>} brokerRows
 * @returns {{ legsByOrder: Map<string, Array<{label: string, id: string, status: string, missing: boolean, trigger_values: number[]}>>,
 *             unmatched: Array<object> }}
 */
export function matchGtts(orders, brokerRows) {
  const byId = new Map();
  for (const r of brokerRows || []) {
    if (r && r.gtt_id != null) byId.set(String(r.gtt_id), r);
  }
  const referenced = new Set();
  const legsByOrder = new Map();

  for (const o of orders || []) {
    const oid = String(o.order_id ?? o.id ?? '');
    const legs = [];
    for (const e of parseAttached(o.attached_gtts_json)) {
      if (!e || e.kind !== 'gtt') continue;
      const id = e.id != null ? String(e.id) : '';
      const row = id ? byId.get(id) : undefined;
      if (id) referenced.add(id);
      legs.push({
        label: e.label || 'gtt',
        id,
        status: row ? gttStatusLabel(row.status) : 'missing',
        missing: !row,
        trigger_values: row?.trigger_values ?? [],
      });
    }
    if (legs.length) legsByOrder.set(oid, legs);
  }

  const unmatched = (brokerRows || []).filter(r => r && !referenced.has(String(r.gtt_id)));
  return { legsByOrder, unmatched };
}
