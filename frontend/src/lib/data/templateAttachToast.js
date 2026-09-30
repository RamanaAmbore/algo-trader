/**
 * templateAttachToast.js — decides when to fire a one-time toast for an
 * order whose template selection never attached (TP/SL/Wing GTTs).
 *
 * OrderCard.svelte already renders this state inline (the `tmpl:#N ⟳`
 * chip + Re-attach button, gated on
 * `template_id != null && status === 'FILLED' && !attached_gtts_json`)
 * but that's only visible while the operator is looking at the Order
 * Activity book. This module adds a short-lived toast so the operator
 * notices without having to go look — called from the POLL LOOP (
 * `OrderBook.svelte` / `LogPanel.svelte`'s `_loadOrders`, on the merged
 * rows BEFORE any status-chip filtering), not from OrderCard itself —
 * OrderCard only ever mounts for rows the currently-selected status
 * chip happens to match, so wiring detection to its mount/effect
 * lifecycle would silently never fire for rows filtered out of view
 * (confirmed live: OrderBook's own "Filled" chip predicate checks
 * broker-vocabulary `status === 'COMPLETE'`, but AlgoOrder rows — the
 * ONLY rows that ever carry `template_id` — use `'FILLED'`
 * (`ALGO_ORDER_FINAL_STATUSES` in `backend/api/models.py`); see the
 * "OrderBook FILLED-status chip gap" note in this fix's task report for
 * that separate, pre-existing, out-of-scope defect).
 *
 * Two design constraints drive the shape of `noteAttachObservation`:
 *
 * 1. LIVE-only. `_fire_template_attach_on_fill` (backend/api/routes/
 *    orders_place.py) is a hard no-op for any `mode` other than
 *    `'live'` — a paper/sim/replay/shadow fill NEVER attempts a real
 *    broker GTT/wing attach, by design (those modes have no real
 *    broker position to protect). For those modes, "FILLED + template
 *    selected + attached_gtts_json still null" is the PERMANENT resting
 *    state, not a failure — `isAttachFailedState` returns `false`
 *    outright unless `order.mode` is (case-insensitively) `'live'`.
 *
 * 2. Template attach runs ASYNCHRONOUSLY after the parent order's
 *    FILLED status is written (`asyncio.create_task` in the postback
 *    handler, then a real broker GTT/wing placement round-trip) — a
 *    poll landing in that gap looks IDENTICAL to a genuine silent-drop
 *    failure. Fixed with a TIME-based debounce (not a poll-count
 *    debounce, which breaks the moment more than one surface polls the
 *    same order concurrently — e.g. the order modal's bottom OrderBook
 *    AND the /orders page's own card, both live at once): the failed
 *    state must persist for at least `ATTACH_STALL_THRESHOLD_MS`
 *    (15s — matches this app's own established "beyond this, treat as
 *    genuinely stuck" convention, see api.js's identical 15s
 *    AbortController submit timeout) before it toasts, timed from the
 *    FIRST observation across ALL callers (sessionStorage-backed, so
 *    concurrent pollers share one clock regardless of their own poll
 *    cadence).
 *
 * De-duped per order_id via sessionStorage so a toast fires at most
 * once per order per browser tab session (survives OrderCard/OrderBook
 * remounts — e.g. modal close/reopen — but resets on a fresh tab/
 * reload, same tradeoff the existing `rbq.reattach-fail.<id>` pattern
 * in OrderCard.svelte accepts).
 */

/** Failed-attach state must persist this long (from first observation)
 *  before it's treated as a genuine stall rather than the normal async
 *  attach-latency window. Exported so tests/tuning share one constant. */
export const ATTACH_STALL_THRESHOLD_MS = 15_000;

const _FIRST_SEEN_PREFIX = 'rbq.attach-fail-first-seen.';
const _TOASTED_PREFIX = 'rbq.attach-fail-toasted.';

function _ss() {
  try {
    return typeof sessionStorage !== 'undefined' ? sessionStorage : null;
  } catch {
    return null;
  }
}

// In-memory fallback for environments without sessionStorage (SSR,
// private-mode storage exceptions). Module-level so it still de-dupes
// across callers within the same page lifetime.
const _memFirstSeen = new Map();
const _memToasted = new Set();

/**
 * True when an order's current fields look like a stalled template
 * attach: a LIVE-mode order selected a template, reached FILLED, and
 * no GTT specs ever landed. Always `false` for non-live modes — see
 * constraint 1 in the module header.
 * @param {{ id?: any, order_id?: any, template_id?: any, status?: string, attached_gtts_json?: any, mode?: string }|null|undefined} order
 * @returns {boolean}
 */
export function isAttachFailedState(order) {
  if (!order) return false;
  if (String(order.mode || '').toLowerCase() !== 'live') return false;
  return order.template_id != null
    && String(order.status || '').toUpperCase() === 'FILLED'
    && !order.attached_gtts_json;
}

/**
 * Call on every poll observation of an order (from the poll loop that
 * fetches the merged order rows — see module header for why this must
 * NOT be tied to a filtered view's component mount lifecycle). Returns
 * `true` at most once per order_id — the FIRST observation after the
 * failed-attach state has persisted for `ATTACH_STALL_THRESHOLD_MS` —
 * the caller should fire the toast only when this returns `true`.
 *
 * Recovering (attach lands, mode isn't live, or the row moves out of
 * FILLED) clears the "first seen" clock so a later, genuinely-new
 * failure on the same order_id is timed fresh — but an order_id that
 * already toasted never toasts again this session.
 *
 * @param {{ id?: any, order_id?: any, template_id?: any, status?: string, attached_gtts_json?: any, mode?: string }|null|undefined} order
 * @param {{ now?: number }} [opts] `now` is injectable for tests.
 * @returns {boolean}
 */
export function noteAttachObservation(order, opts = {}) {
  const id = order?.id ?? order?.order_id;
  if (id == null) return false;
  const key = String(id);
  const now = opts.now ?? Date.now();
  const failed = isAttachFailedState(order);
  const ss = _ss();
  const firstSeenKey = _FIRST_SEEN_PREFIX + key;
  const toastedKey = _TOASTED_PREFIX + key;

  if (!failed) {
    if (ss) { try { ss.removeItem(firstSeenKey); } catch { /* ignore */ } }
    _memFirstSeen.delete(key);
    return false;
  }

  if (ss) {
    try {
      if (ss.getItem(toastedKey)) return false;
      const storedFirstSeen = ss.getItem(firstSeenKey);
      if (storedFirstSeen == null) {
        ss.setItem(firstSeenKey, String(now));
        return false;
      }
      if (now - Number(storedFirstSeen) >= ATTACH_STALL_THRESHOLD_MS) {
        ss.setItem(toastedKey, '1');
        return true;
      }
      return false;
    } catch {
      // Storage unavailable (quota / private mode) — fall through to
      // the in-memory path below.
    }
  }

  if (_memToasted.has(key)) return false;
  const memFirstSeen = _memFirstSeen.get(key);
  if (memFirstSeen == null) {
    _memFirstSeen.set(key, now);
    return false;
  }
  if (now - memFirstSeen >= ATTACH_STALL_THRESHOLD_MS) {
    _memToasted.add(key);
    return true;
  }
  return false;
}

/** Test-only reset — clears both sessionStorage and in-memory state for
 *  a given order_id (or everything, when called with no argument). */
export function _resetAttachToastState(id) {
  const ss = _ss();
  if (id == null) {
    _memFirstSeen.clear();
    _memToasted.clear();
    if (ss) {
      try {
        for (let i = ss.length - 1; i >= 0; i--) {
          const k = ss.key(i);
          if (k && (k.startsWith(_FIRST_SEEN_PREFIX) || k.startsWith(_TOASTED_PREFIX))) ss.removeItem(k);
        }
      } catch { /* ignore */ }
    }
    return;
  }
  const key = String(id);
  _memFirstSeen.delete(key);
  _memToasted.delete(key);
  if (ss) {
    try { ss.removeItem(_FIRST_SEEN_PREFIX + key); ss.removeItem(_TOASTED_PREFIX + key); } catch { /* ignore */ }
  }
}
