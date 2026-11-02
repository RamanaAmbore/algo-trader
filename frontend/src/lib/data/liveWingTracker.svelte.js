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
  _tracked = trackedParents;
  return badgeMap;
}
