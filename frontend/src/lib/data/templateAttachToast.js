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
 *
 * 2026-11-02 (PLAN.md item 4) — a HELD-pending-release row (a template-
 * exit hold, `order_hold_gate.py:hold_template_exit`) used to reach this
 * exact same "FILLED + template selected + no attached_gtts_json" shape
 * and, once `ATTACH_STALL_THRESHOLD_MS` elapsed, toast the SAME "did not
 * attach" wording as a genuine failure — factually wrong, since the
 * exit is correctly, deliberately held, not dropped. `isAttachFailedState`
 * now excludes this state (see its own doc comment); a separate,
 * un-debounced `noteHeldPendingRelease()` toasts the correct wording
 * instead. The genuine-failure toast text is UNCHANGED.
 */

import { parseHold } from './holdRecord.js';

/** Failed-attach state must persist this long (from first observation)
 *  before it's treated as a genuine stall rather than the normal async
 *  attach-latency window. Exported so tests/tuning share one constant. */
export const ATTACH_STALL_THRESHOLD_MS = 15_000;

const _FIRST_SEEN_PREFIX = 'rbq.attach-fail-first-seen.';
const _TOASTED_PREFIX = 'rbq.attach-fail-toasted.';
const _HELD_TOASTED_PREFIX = 'rbq.attach-held-toasted.';

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
const _memHeldToasted = new Set();

/**
 * True when an order's current fields look like a stalled template
 * attach: a LIVE-mode order selected a template, reached FILLED, and
 * no GTT specs ever landed. Always `false` for non-live modes — see
 * constraint 1 in the module header.
 *
 * Also always `false` for a GTT/wing CHILD row (`parent_order_id` set).
 * A child leg attached to a parent order can inherit `template_id` from
 * that parent but never gets its own `attached_gtts_json` populated —
 * only the parent row gets that written by `_fire_template_attach_on_fill`
 * (backend). Without this guard, a normal child leg with nothing to
 * attach itself would false-fire as a stalled/failed attach. Only a row
 * with no `parent_order_id` (an actual parent order) can be tracked for
 * attach-completion.
 * @param {{ id?: any, order_id?: any, template_id?: any, status?: string, attached_gtts_json?: any, mode?: string, parent_order_id?: any }|null|undefined} order
 * @returns {boolean}
 *
 * 2026-11-02 (PLAN.md item 4) — also always `false` when the row is
 * HELD pending release (`isHeldPendingReleaseState` below). A
 * template-exit hold leaves a row in this exact same shape — live,
 * template selected, FILLED, no `attached_gtts_json` — PERMANENTLY
 * until the operator releases it from Held Orders, which previously
 * meant it inevitably crossed `ATTACH_STALL_THRESHOLD_MS` and toasted
 * as a genuine failure ("Bracket did not attach") even though the
 * exit was correctly, deliberately held — see
 * `isHeldPendingReleaseState`'s own doc comment for the distinct,
 * non-debounced toast this state gets instead.
 */
export function isAttachFailedState(order) {
  if (!order) return false;
  if (String(order.mode || '').toLowerCase() !== 'live') return false;
  if (order.parent_order_id != null) return false;
  if (isHeldPendingReleaseState(order)) return false;
  return order.template_id != null
    && String(order.status || '').toUpperCase() === 'FILLED'
    && !order.attached_gtts_json;
}

/**
 * True when a parent order's Bracket exit is correctly, deliberately
 * HELD pending operator release (`order_hold_gate.py:hold_template_exit`)
 * — NOT a genuine attach failure. The backend writes `hold_json` with
 * `category: 'template_exit'` synchronously, in the same step that
 * decides to hold the exit (see `hold_template_exit()` in
 * `order_hold_gate.py`) — unlike a genuine attach failure, there is no
 * race window here, so this never needs the time-based debounce
 * `isAttachFailedState`/`noteAttachObservation` use.
 *
 * Same field gating as `isAttachFailedState` (live, no `parent_order_id`,
 * template selected, FILLED, no `attached_gtts_json` yet) PLUS the
 * `hold_json` category check — a row can be in exactly one of these two
 * states, never both (see the guard added to `isAttachFailedState`
 * above).
 * @param {{ template_id?: any, status?: string, attached_gtts_json?: any, mode?: string, parent_order_id?: any, hold_json?: string|null }|null|undefined} order
 * @returns {boolean}
 */
export function isHeldPendingReleaseState(order) {
  if (!order) return false;
  if (String(order.mode || '').toLowerCase() !== 'live') return false;
  if (order.parent_order_id != null) return false;
  if (order.template_id == null) return false;
  if (String(order.status || '').toUpperCase() !== 'FILLED') return false;
  if (order.attached_gtts_json) return false;
  return parseHold(order.hold_json)?.category === 'template_exit';
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
 * @param {{ id?: any, order_id?: any, template_id?: any, status?: string, attached_gtts_json?: any, mode?: string, parent_order_id?: any }|null|undefined} order
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

/**
 * Call on every poll observation of an order (same poll loop as
 * `noteAttachObservation` — see this file's module header). Returns
 * `true` at most once per order_id, the FIRST time the row is observed
 * in `isHeldPendingReleaseState`. Unlike `noteAttachObservation`, this
 * is NOT time-debounced — a template-exit hold is written synchronously
 * at fill time (no async-attach race to wait out), so the first
 * observation IS the real state, not a suspected stall.
 *
 * Recovering (released — attach lands, or the hold is lifted) clears
 * the toasted flag so a LATER, genuinely-new hold on the same order_id
 * (e.g. re-held after a failed release attempt) toasts again — mirrors
 * `noteAttachObservation`'s own recovery semantics.
 *
 * @param {{ id?: any, order_id?: any, template_id?: any, status?: string, attached_gtts_json?: any, mode?: string, parent_order_id?: any, hold_json?: string|null }|null|undefined} order
 * @returns {boolean}
 */
export function noteHeldPendingRelease(order) {
  const id = order?.id ?? order?.order_id;
  if (id == null) return false;
  const key = String(id);
  const held = isHeldPendingReleaseState(order);
  const ss = _ss();
  const toastedKey = _HELD_TOASTED_PREFIX + key;

  if (!held) {
    if (ss) { try { ss.removeItem(toastedKey); } catch { /* ignore */ } }
    _memHeldToasted.delete(key);
    return false;
  }

  if (ss) {
    try {
      if (ss.getItem(toastedKey)) return false;
      ss.setItem(toastedKey, '1');
      return true;
    } catch {
      // Storage unavailable (quota / private mode) — fall through.
    }
  }

  if (_memHeldToasted.has(key)) return false;
  _memHeldToasted.add(key);
  return true;
}

/** Test-only reset — clears both sessionStorage and in-memory state for
 *  a given order_id (or everything, when called with no argument). */
export function _resetAttachToastState(id) {
  const ss = _ss();
  if (id == null) {
    _memFirstSeen.clear();
    _memToasted.clear();
    _memHeldToasted.clear();
    if (ss) {
      try {
        for (let i = ss.length - 1; i >= 0; i--) {
          const k = ss.key(i);
          if (k && (k.startsWith(_FIRST_SEEN_PREFIX) || k.startsWith(_TOASTED_PREFIX) || k.startsWith(_HELD_TOASTED_PREFIX))) ss.removeItem(k);
        }
      } catch { /* ignore */ }
    }
    return;
  }
  const key = String(id);
  _memFirstSeen.delete(key);
  _memToasted.delete(key);
  _memHeldToasted.delete(key);
  if (ss) {
    try {
      ss.removeItem(_FIRST_SEEN_PREFIX + key);
      ss.removeItem(_TOASTED_PREFIX + key);
      ss.removeItem(_HELD_TOASTED_PREFIX + key);
    } catch { /* ignore */ }
  }
}
