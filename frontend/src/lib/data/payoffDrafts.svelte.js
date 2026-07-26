// Session-only in-memory store for payoff draft positions.
// Populated from the OrderTicket "Add to Payoff" button.
// Cleared on page refresh; never persisted to DB or localStorage.
//
// Distinct from:
//   - draftPositions.svelte.js (provisional fill-tracking, post-broker-fill rows)
//   - the derivatives page's local `drafts` state (text-input form rows)
//
// Each entry is shaped to match the derivatives page's local draft shape:
//   { id: string, symbol: string, qty: number (signed), avg_cost: number|null, ltp: '' }
// so it can be merged into `buildCandidatePositions({ drafts })` without modification.
// BUY qty is positive, SELL qty is negative (same convention as the local drafts).
//
// Extended fields for order-status card integration:
//   transaction_type: 'BUY' | 'SELL'  — derived from qty sign
//   option_type: 'CE' | 'PE' | null
//   strike: number | null
//   expiry: string | null
//   is_draft: true                     — distinguishes from real orders

let _seq = 0;

/**
 * @type {Map<string, {
 *   id: string,
 *   symbol: string,
 *   qty: number,
 *   avg_cost: number | null,
 *   ltp: string,
 *   underlying: string,
 *   exchange: string,
 *   transaction_type: 'BUY' | 'SELL',
 *   option_type: 'CE' | 'PE' | null,
 *   strike: number | null,
 *   expiry: string | null,
 *   is_draft: true,
 * }>}
 */
let _map = $state(new Map());

export const payoffDrafts = {
  /** Reactive Map — read in $derived to get live updates. */
  get value() { return _map; },

  /**
   * Add a draft payoff leg.
   * qty should be signed: positive for BUY, negative for SELL.
   *
   * @param {{
   *   symbol: string,
   *   exchange: string,
   *   qty: number,
   *   avg_cost?: number | null,
   *   underlying?: string,
   *   option_type?: 'CE' | 'PE' | null,
   *   strike?: number | null,
   *   expiry?: string | null,
   * }} entry
   * @returns {string} stable id for later removal
   */
  add(entry) {
    const id = `pd_${++_seq}_${Math.random().toString(36).slice(2, 7)}`;
    const signedQty = Number(entry.qty || 0);
    const next = new Map(_map);
    next.set(id, {
      id,
      symbol:           String(entry.symbol     || '').toUpperCase(),
      exchange:         String(entry.exchange   || ''),
      qty:              signedQty,
      avg_cost:         entry.avg_cost != null  ? Number(entry.avg_cost) : null,
      ltp:              '',
      underlying:       String(entry.underlying || ''),
      transaction_type: signedQty >= 0 ? 'BUY' : 'SELL',
      option_type:      entry.option_type ?? null,
      strike:           entry.strike != null ? Number(entry.strike) : null,
      expiry:           entry.expiry ?? null,
      is_draft:         /** @type {true} */ (true),
    });
    _map = next;
    return id;
  },

  /** Remove a draft by its id (no-op if not found). */
  remove(/** @type {string} */ id) {
    if (!_map.has(id)) return;
    const next = new Map(_map);
    next.delete(id);
    _map = next;
  },

  /** Remove all payoff drafts. */
  clear() {
    _map = new Map();
  },
};
