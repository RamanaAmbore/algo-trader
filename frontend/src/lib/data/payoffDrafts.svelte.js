// Server-backed cache for payoff draft positions (2026-10).
// Populated from the OrderTicket "Add to Payoff" button; persisted via
// the /api/orders/drafts CRUD endpoints (commit 61a9dd39) so a draft
// survives a page refresh instead of living only in module-level $state.
//
// Distinct from:
//   - draftPositions.svelte.js (provisional fill-tracking, post-broker-fill rows)
//   - the derivatives page's local `drafts` state (text-input form rows —
//     a completely separate client-only concept that never reaches this
//     store or the backend)
//
// Each entry is shaped to match the derivatives page's local draft shape:
//   { id: number, symbol: string, qty: number (signed), avg_cost: number|null, ltp: '' }
// so it can be merged into `buildCandidatePositions({ drafts })` without modification.
// BUY qty is positive, SELL qty is negative (same convention as the local drafts).
//
// Extended fields for order-status card integration:
//   transaction_type: 'BUY' | 'SELL'  — derived from qty sign
//   option_type: 'CE' | 'PE' | null   — derived from symbol text (decomposeSymbol)
//   strike: number | null             — derived from symbol text
//   expiry: string | null             — not resolved by this store (see
//                                        decomposeSymbol if a future caller needs it)
//   account: string                   — '' means unassigned
//   is_draft: true                     — distinguishes from real orders
//
// id convention: the REAL backend AlgoOrder.id (mode='draft' row), not a
// locally-generated id — remove()/update() reference this id against the
// backend directly.

import { fetchDrafts, createDraft, updateDraft, deleteDraft } from '$lib/api';
// Pure logic (row↔entry conversion) lives in payoffDraftHelpers.js, split
// out specifically so it's importable + unit-testable in Vitest without
// the Svelte compiler (this file's module-level $state calls below would
// throw `$state is not defined` on a plain Node import) — see
// payoffDraftHelpers.test.js.
import { draftRowToEntry, draftEntryToBody } from './payoffDraftHelpers.js';

/** True once load() has resolved at least once (success OR failure) —
 *  lets a consumer avoid treating an empty Map as "no draft exists yet"
 *  before the initial fetch has actually landed. */
let _loaded = $state(false);

/**
 * @type {Map<number, {
 *   id: number,
 *   symbol: string,
 *   qty: number,
 *   avg_cost: number | null,
 *   ltp: string,
 *   underlying: string,
 *   exchange: string,
 *   account: string,
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

  /** True once an initial load() has resolved (success or failure). */
  get loaded() { return _loaded; },

  /**
   * Fetch every draft row from the backend (GET /api/orders/drafts) and
   * replace the local Map with the result. Call once per page mount
   * (orders page / derivatives page onMount) so drafts survive a
   * refresh. Leaves the existing Map untouched on a fetch failure —
   * a transient network blip must not wipe an already-populated cache.
   */
  async load() {
    try {
      const rows = await fetchDrafts();
      const next = new Map();
      for (const row of (rows || [])) {
        const entry = draftRowToEntry(row);
        next.set(entry.id, entry);
      }
      _map = next;
    } catch (_) {
      // Already logged via api.js's console.warn chokepoint — leave
      // the current Map as-is rather than poisoning it with empty.
    } finally {
      _loaded = true;
    }
  },

  /**
   * Add a draft payoff leg. Persists via POST /api/orders/drafts and
   * stores the entry under the real backend-assigned id.
   * qty should be signed: positive for BUY, negative for SELL.
   *
   * @param {{
   *   symbol: string,
   *   exchange: string,
   *   qty: number,
   *   avg_cost?: number | null,
   *   underlying?: string,
   *   account?: string | null,
   *   option_type?: 'CE' | 'PE' | null,
   *   strike?: number | null,
   *   expiry?: string | null,
   * }} entry
   * @returns {Promise<number|null>} the real backend id, or null on failure
   */
  async add(entry) {
    const body = draftEntryToBody(entry);
    const resp = await createDraft(body);
    const id = resp?.id;
    if (id == null) return null;
    const next = new Map(_map);
    next.set(id, draftRowToEntry({ ...body, id, initial_price: body.price }));
    _map = next;
    return id;
  },

  /**
   * Update an existing draft in place (PATCH /api/orders/drafts/{id})
   * instead of remove-then-add — keeps the same id so any UI state that
   * remembered it (e.g. an open OrderTicket's initialDraftId) stays valid.
   *
   * @param {number} id
   * @param {{symbol: string, exchange: string, qty: number,
   *   avg_cost?: number | null, account?: string | null}} entry
   */
  async update(id, entry) {
    const body = draftEntryToBody(entry);
    const resp = await updateDraft(id, body);
    if (!resp) return;
    const next = new Map(_map);
    next.set(resp.id, draftRowToEntry(resp));
    _map = next;
  },

  /**
   * Remove a draft by its id — calls DELETE /api/orders/drafts/{id} and
   * clears the local entry on success. A 404 means the row is already
   * gone (e.g. a race with the ticket-success server-side cleanup) and
   * is treated as success, not an operator-visible error. Any other
   * failure leaves the local entry untouched (already logged by
   * api.js's chokepoint) so the UI doesn't silently desync from the
   * backend.
   *
   * @param {number} id
   */
  async remove(id) {
    if (!_map.has(id)) return;
    try {
      await deleteDraft(id);
    } catch (e) {
      if (/** @type {any} */ (e)?.status !== 404) return;
    }
    const next = new Map(_map);
    next.delete(id);
    _map = next;
  },

  /**
   * Remove a draft from the LOCAL cache only — no DELETE request. Used
   * after a real ticket submission succeeds with a `draft_id`: the
   * backend already deleted the row server-side (see
   * TicketOrderRequest.draft_id's post-success cleanup), so issuing a
   * DELETE here too would just 404 for no benefit.
   *
   * @param {number} id
   */
  forget(id) {
    if (!_map.has(id)) return;
    const next = new Map(_map);
    next.delete(id);
    _map = next;
  },

  /** Remove all payoff drafts from the local cache only (no backend call). */
  clear() {
    _map = new Map();
  },
};
