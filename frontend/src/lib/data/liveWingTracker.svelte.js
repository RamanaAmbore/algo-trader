/**
 * liveWingTracker.svelte.js — session-local tracking of Chain-tab basket
 * legs this browser tab has submitted LIVE, so `OptionChainTab.svelte`
 * can show a read-only "wing fired live" confirmation badge once the
 * backend's asynchronous template-attach places the wing/hedge leg on
 * parent fill.
 *
 * Module-level `$state` — written by `SymbolPanel.svelte`'s
 * `submitBasket()` on a successful LIVE leg placement (the moment it
 * has the broker's own order id back), read by `OptionChainTab.svelte`
 * (which also owns pruning, via `refreshLiveWingState`, run on every
 * existing orders-poll tick). Survives Chain modal close/reopen within
 * the same tab (module-level, not component-local) but NOT a page
 * reload — this is an explicitly session-scoped confirmation UI, not a
 * persisted record. OrderBook / the Positions grid remain the
 * permanent record of what's actually live.
 *
 * Never reads or writes `chainBasket`/`basketLegs` — see
 * `liveWingBadge.js`'s module docstring for why that boundary matters.
 *
 * ROOT CAUSE OF THE 2026-11-03 LIVE INCIDENT (Chain tab hangs the
 * instant it is pressed, modal close button unresponsive) — fixed here,
 * NOT where the original attempted fix (commit 80a47cbd) looked.
 * `refreshLiveWingState()` used to write `_tracked = trackedParents`
 * UNCONDITIONALLY on every call. `computeLiveWingState()` (in
 * liveWingBadge.js) always builds `nextTracked` as a BRAND-NEW array —
 * `const nextTracked = []`, pushed into — even when the input is empty
 * and nothing changed. A brand-new array is never `===` the old one, so
 * the `$effect` in OptionChainTab.svelte that reads this state (via this
 * function) and writes it back in the SAME tick saw `_tracked` as
 * "changed" on every single call, including the baseline case where the
 * operator had never placed a live Bracket order from Chain that
 * session (`_tracked` starts as `[]`; `[] !== []` by reference). That is
 * a synchronous, unconditional infinite effect loop starting the instant
 * the Chain tab mounts — it pegs the main thread solid, which is exactly
 * "hangs the moment pressed" and an unresponsive close button (the
 * thread never comes back to handle the click). The original fix
 * (80a47cbd) guarded the OUTER `_liveWingByKey` reassignment in
 * OptionChainTab.svelte, but this INNER write ran unconditionally
 * regardless of that guard, so the loop never actually stopped — this is
 * why that fix did not resolve the reported hang.
 *
 * Fix: only reassign `_tracked` when its content has genuinely changed —
 * same length AND every entry's `brokerOrderId`/`lastSeenAt` identical —
 * not just the "both empty" shortcut. A non-empty steady state has the
 * exact same unconditional-spread-copy shape once an operator has 1+
 * tracked parent, so the equality check must cover that case too, not
 * only the empty one that caused this particular incident.
 */
import { computeLiveWingState } from './liveWingBadge.js';
import { decomposeSymbol } from './decomposeSymbol.js';

/** @typedef {{account:string, symbol:string, brokerOrderId:string, templateName:string, submittedAt:number, lastSeenAt:number}} TrackedParent */

/** @type {TrackedParent[]} */
let _tracked = $state([]);

/**
 * Record a just-submitted LIVE basket leg's broker order id so a later
 * poll can recognize its wing (if any) firing. No-op without a
 * `brokerOrderId` (nothing to match against later).
 *
 * @param {{account?:string, symbol:string, brokerOrderId:string|number|null|undefined, templateName?:string}} entry
 */
export function trackChainParentSubmission(entry) {
  if (!entry || entry.brokerOrderId == null || entry.brokerOrderId === '') return;
  const now = Date.now();
  _tracked = [..._tracked, {
    account: entry.account || '',
    symbol: entry.symbol || '',
    brokerOrderId: String(entry.brokerOrderId),
    templateName: entry.templateName || '',
    submittedAt: now,
    lastSeenAt: now,
  }];
}

/** Read-only accessor for the current tracked-parent list — exposed
 *  mainly for tests; `refreshLiveWingState` is the real consumer. */
export function getTrackedChainParents() {
  return _tracked;
}

/** Test-only reset. */
export function _resetLiveWingTracker() {
  _tracked = [];
}

/**
 * True iff `a` and `b` are the same length and every entry's
 * `brokerOrderId`/`lastSeenAt` pair matches at the same index — the
 * fields `computeLiveWingState()` actually mutates. A structural
 * equality check, not a reference check, since `nextTracked` is always
 * a fresh array/fresh objects even when nothing meaningful changed.
 *
 * @param {TrackedParent[]} a
 * @param {TrackedParent[]} b
 */
function _trackedListsEqual(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i].brokerOrderId !== b[i].brokerOrderId) return false;
    if (a[i].lastSeenAt !== b[i].lastSeenAt) return false;
  }
  return true;
}

/**
 * Recompute the live-wing badge map against the latest orders-poll rows
 * and prune the tracked-parent list in place. Call from an `$effect` in
 * `OptionChainTab.svelte` whenever the poll's rows (or the on-screen
 * underlying/expiry) change — see `computeLiveWingState`'s own
 * docstring for exactly what "match" means.
 *
 * @param {{
 *   algoRows: any[],
 *   chainUnderlying: string,
 *   chainExpiry: string,
 *   findOption: (underlying:string, optType:string, strike:number, expiry:string) => any,
 *   getInstrument: (sym:string) => any,
 *   nowMs?: number,
 * }} args
 * @returns {Map<string, any>}
 */
export function refreshLiveWingState({ algoRows, chainUnderlying, chainExpiry, findOption, getInstrument, nowMs }) {
  const { badgeMap, trackedParents } = computeLiveWingState({
    trackedParents: _tracked,
    algoRows,
    chainUnderlying,
    chainExpiry,
    decompose: decomposeSymbol,
    findOption,
    getInstrument,
    nowMs,
  });
  // THE FIX — only write back when something genuinely changed. Skipping
  // this (the pre-incident code always wrote) is what breaks the
  // effect's own read-then-write-the-same-state cycle: no real change in
  // -> no write -> no reschedule -> the effect settles instead of
  // spinning forever. A genuine state transition (a parent tracked,
  // pruned, or a wing resolving) still always writes.
  if (!_trackedListsEqual(_tracked, trackedParents)) {
    _tracked = trackedParents;
  }
  return badgeMap;
}
