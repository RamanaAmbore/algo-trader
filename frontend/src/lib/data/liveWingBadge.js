/**
 * liveWingBadge.js — pure resolver for the Chain tab's read-only "wing
 * fired live" confirmation badge.
 *
 * Context: a Bracket template's wing/hedge leg is placed ASYNCHRONOUSLY,
 * on the PARENT order's fill, by the backend's
 * `_fire_template_attach_on_fill` -> `apply_template_to_order` ->
 * `_ta_live_place_wing`/`_chase_wing` (backend/api/algo/template_attach.py).
 * This is NEVER at Submit time — the operator submits the parent leg,
 * the wing shows up later, independently, once the parent actually
 * fills. This module answers "did a wing I submitted from Chain this
 * session actually fire?" by matching the Chain tab's own
 * session-local tracking of recently-submitted parents against the
 * existing orders poll (`orderFillPoller.js`'s `latestAlgoOrderRows`).
 *
 * This is READ-ONLY / display-only. It must NEVER write into
 * `chainBasket`/`basketLegs` — a confirmed-live wing leg must never be
 * able to re-enter the pre-submit staging array, or the next Submit
 * click could try to re-place it as a duplicate live order. See
 * `OptionChainTab.svelte`'s `_liveWingByKey` (kept entirely separate
 * from `_basketLegByKey`) and `SymbolPanel.svelte`'s `submitBasket()`.
 *
 * Wing-row discriminator: `AlgoOrder.source === 'template_wing'` AND
 * `parent_order_id != null` (set exclusively by the LIMIT/chase-routed
 * wing path, `_chase_wing` in template_attach.py — the only wing shape
 * that gets its own trackable AlgoOrder row; a direct MARKET wing has
 * no such row and is out of scope for this badge, see CLAUDE.md's
 * `_ta_live_place_wing` notes). This is deliberately NOT a bare
 * `parent_order_id === X` filter — `parent_order_id` is also set on
 * take-profit children (`source === 'take_profit'`, same contract as
 * the parent) and must never be mistaken for a wing/hedge leg.
 */

/** How long a tracked parent submission is kept alive with no activity
 *  (no parent resolution, no wing match) before it's dropped. Generous
 *  — a low-aggressiveness chase can run ~30 attempts x 30s =~15 min
 *  before it fills, so 30 min covers that with real margin. Refreshed
 *  (not just checked) every time the entry sees real activity, so a
 *  wing that stays live/open keeps its tracked parent — and therefore
 *  its badge — alive for as long as it's actually being observed. */
export const WING_TRACK_PRUNE_MS = 30 * 60 * 1000;

/** AlgoOrder.source value stamped exclusively on a chase-routed wing
 *  leg's own row (`_chase_wing` in template_attach.py). */
const WING_SOURCE = 'template_wing';

/** Statuses for which the badge is shown. Anything else (REJECTED,
 *  CANCELLED, CANCEL_FAILED, UNFILLED) means "resolved to a dead end" —
 *  the tracked entry is dropped immediately rather than waiting out
 *  the TTL, so a failed/cancelled wing never lingers as a stale badge. */
const VISIBLE_STATUSES = new Set(['OPEN', 'FILLED', 'COMPLETE']);

/**
 * @param {{brokerOrderId:string}} tracked
 * @param {any[]} rows
 * @returns {number|null} the matching parent AlgoOrder's own `id`, or
 *   null when no row's `broker_order_id` (current or superseded)
 *   matches the broker id captured at submit time.
 */
function _resolveParentAlgoId(tracked, rows) {
  for (const row of rows) {
    if (!row) continue;
    const bid = String(row.broker_order_id ?? '');
    if (bid && bid === tracked.brokerOrderId) return row.id;
    const superseded = Array.isArray(row.superseded_broker_order_ids)
      ? row.superseded_broker_order_ids : [];
    if (superseded.some((x) => String(x) === tracked.brokerOrderId)) return row.id;
  }
  return null;
}

/**
 * @param {number|null} parentAlgoId
 * @param {any[]} rows
 * @returns {any|null} the wing child row, or null.
 */
function _findLiveWingRow(parentAlgoId, rows) {
  if (parentAlgoId == null) return null;
  for (const row of rows) {
    if (!row) continue;
    if (row.source !== WING_SOURCE) continue;
    if (row.parent_order_id == null) continue;
    if (String(row.parent_order_id) !== String(parentAlgoId)) continue;
    return row;
  }
  return null;
}

/**
 * Resolve the Chain tab's live-wing badge map for the strikes currently
 * on screen, and the next (pruned/refreshed) tracked-parents array.
 *
 * Pure — takes every lookup as an injected function so it's testable
 * with zero Svelte/instruments-cache dependency, and so the caller (the
 * real `getInstrument`/`findOption` from `$lib/data/instruments`,
 * `decomposeSymbol` from this same data/ directory) is never
 * reimplemented here.
 *
 * @param {{
 *   trackedParents: Array<{account:string, symbol:string, brokerOrderId:string, templateName:string, submittedAt:number, lastSeenAt?:number}>,
 *   algoRows: any[],
 *   chainUnderlying: string,
 *   chainExpiry: string,
 *   decompose: (sym:string) => {strike:number|null, optType:'CE'|'PE'|null},
 *   findOption: (underlying:string, optType:string, strike:number, expiry:string) => any,
 *   getInstrument: (sym:string) => any,
 *   nowMs?: number,
 * }} args
 * @returns {{ badgeMap: Map<string, {lots:number, templateName:string, side:string, algoOrderId:number, status:string}>, trackedParents: any[] }}
 */
export function computeLiveWingState({
  trackedParents, algoRows, chainUnderlying, chainExpiry,
  decompose, findOption, getInstrument, nowMs = Date.now(),
}) {
  const rows = Array.isArray(algoRows) ? algoRows : [];
  const badgeMap = /** @type {Map<string, any>} */ (new Map());
  const nextTracked = [];

  for (const tracked of (Array.isArray(trackedParents) ? trackedParents : [])) {
    if (!tracked || !tracked.brokerOrderId) continue;
    let lastSeenAt = tracked.lastSeenAt ?? tracked.submittedAt ?? nowMs;
    let drop = false;

    const parentAlgoId = _resolveParentAlgoId(tracked, rows);
    if (parentAlgoId != null) lastSeenAt = nowMs;

    const wingRow = _findLiveWingRow(parentAlgoId, rows);
    if (wingRow) {
      const status = String(wingRow.status || '').toUpperCase();
      if (!VISIBLE_STATUSES.has(status)) {
        // Resolved to a dead end (rejected/cancelled/unfilled) — stop
        // tracking immediately rather than waiting out the TTL.
        drop = true;
      } else {
        lastSeenAt = nowMs;
        const sym = String(wingRow.symbol || '');
        const d = /** @type {{strike:number|null, optType:('CE'|'PE'|null)}} */ (decompose(sym) || {});
        if (d.strike != null && d.optType && chainUnderlying && chainExpiry) {
          // Scope guard — the wing's own resolved contract for THIS
          // chain's underlying+expiry must be the SAME instrument the
          // wing row actually carries. Without this, a same-strike
          // same-opt-type contract on a different underlying or a
          // different expiry could badge the wrong row on screen.
          const inst = findOption(String(chainUnderlying).toUpperCase(), d.optType, d.strike, chainExpiry);
          if (inst && String(inst.s) === sym) {
            const lotSize = Number(getInstrument?.(sym)?.ls || inst.ls || 1) || 1;
            const qty = Number(wingRow.quantity || 0);
            const lots = lotSize > 0 ? Math.max(1, Math.round(qty / lotSize)) : qty;
            badgeMap.set(`${d.strike}:${d.optType}`, {
              lots,
              templateName: tracked.templateName || '',
              side: String(wingRow.transaction_type || ''),
              algoOrderId: wingRow.id,
              status: wingRow.status,
            });
          }
        }
      }
    }

    if (!drop && (nowMs - lastSeenAt) <= WING_TRACK_PRUNE_MS) {
      nextTracked.push({ ...tracked, lastSeenAt });
    }
  }

  return { badgeMap, trackedParents: nextTracked };
}
