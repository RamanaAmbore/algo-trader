/**
 * draftOrderRow.js — single predicate for "is this AlgoOrder row a draft?"
 *
 * Drafts are real `AlgoOrder` rows (`mode='draft', status='OPEN'`, never
 * placed at a broker — see `backend/api/routes/orders.py`'s own comment
 * on the `/drafts` CRUD routes). They reach OrderBook.svelte, LogPanel.svelte,
 * and OrderCard.svelte via `fetchAlgoOrdersRecent()`, rendering and
 * counting exactly like a genuine resting broker order under "Open" unless
 * every consumer excludes them (2026-10 round 2 fix).
 *
 * Centralized here so a future status/count fix can never apply to one
 * surface and miss the other — the exact drift risk the risk council
 * flagged when this fix was scoped to BOTH OrderBook.svelte AND
 * LogPanel.svelte in the same commit.
 *
 * Deliberately distinct from `source === 'draft'` used by MarketPulse's
 * Positions grid / the derivatives Legs grid — those operate on a
 * different, position-shaped row (`draftEntryToPositionRow` in
 * draftRowMerge.js), not this AlgoOrder shape.
 *
 * @param {{mode?: string | null} | Record<string, any> | null | undefined} o
 * @returns {boolean}
 */
export function isDraftOrderRow(o) {
  return String(o?.mode || '').toLowerCase() === 'draft';
}
