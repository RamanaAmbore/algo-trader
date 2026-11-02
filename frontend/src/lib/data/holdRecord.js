/**
 * holdRecord.js — parse `AlgoOrder.hold_json` (see
 * `backend/api/algo/order_hold.py:hold_record` / `parse_hold_record`).
 *
 * Extracted (2026-11-02, PLAN.md item 4) from a local copy in
 * `OrderCard.svelte` so `OrderCard.svelte`'s hold display and
 * `templateAttachToast.js`'s toast-wording decision read the exact same
 * parsed shape and can never silently disagree on what "held" means —
 * the same SSOT reasoning `draftOrderRow.js` already documents for its
 * own single predicate.
 *
 * Shape (from the backend's `hold_record()`): `{category, reason,
 * price_policy, override, held_at}`. `category` is one of
 * `HoldCategory`'s values — `'expiry_close'`, `'template_exit'`,
 * `'agent_order'` — as a plain string (the Python Enum's `.value`).
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
