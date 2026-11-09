/**
 * holdRecord.js — parse `AlgoOrder.hold_json` (see
 * `backend/api/algo/order_hold.py:hold_record` / `parse_hold_record`).
 *
 * Extracted (2026-11-02) from a local copy in `OrderCard.svelte` so every
 * hold-displaying surface reads the exact same parsed shape and can
 * never silently disagree on what "held" means — the same SSOT
 * reasoning `draftOrderRow.js` already documents for its own single
 * predicate. Generic across every hold category (`EXPIRY_CLOSE`,
 * `AGENT_ORDER`, `AGENT_CLOSE`) — unlike `templateAttachToast.js`
 * (deleted 2026-11, Track 3 T3.4 Bracket/Template/GTT removal), this
 * module is NOT Bracket-specific and was kept.
 *
 * Shape (from the backend's `hold_record()`): `{category, reason,
 * price_policy, override, held_at}`. `category` is one of
 * `HoldCategory`'s values — `'expiry_close'`, `'agent_order'`,
 * `'agent_close'` — as a plain string (the Python Enum's `.value`).
 * `'template_exit'` was a real value until T3.1 (Bracket/Template/GTT
 * removal) deleted that category server-side; a value this parser has
 * never needed to know about by name, since it is a pure pass-through.
 *
 * @param {string | null | undefined} raw
 * @returns {{category?: string, reason?: string, price_policy?: string, override?: boolean|null, held_at?: string} | null}
 */
export function parseHold(raw) {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch {
    return null;
  }
}
