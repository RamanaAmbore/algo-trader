/**
 * positionsDayPnlStore — backward-compat shim.
 * Delegates to positionsDerivedStore; exposes the legacy numeric API so
 * existing consumers (NavCard, NavBreakdown, MarketPulse) need no changes.
 *
 *   .total         → number  (= positionsDerivedStore.total.day_pnl)
 *   .byKey[sym]    → number  (= positionsDerivedStore.byKey[sym]?.day_pnl ?? 0)
 *   .setFromPulse  → no-op
 */
import { positionsDerivedStore } from '$lib/data/positionsDerivedStore.svelte.js';
export { holdingsDayPnlStore } from '$lib/data/holdingsDayPnlStore.svelte.js';

const _byKeyProxy = new Proxy({}, {
  get(_t, sym) {
    if (typeof sym !== 'string') return undefined;
    return positionsDerivedStore.byKey[sym]?.day_pnl ?? 0;
  },
  has(_t, sym) { return sym in positionsDerivedStore.byKey; },
  // ownKeys + getOwnPropertyDescriptor required so Object.entries/keys work.
  // Without these, Object.entries returns [] (target is {}) — breaking
  // _fnoDayPnlByRoot in the derivatives page which iterates byKey.
  ownKeys(_t) { return Object.keys(positionsDerivedStore.byKey); },
  getOwnPropertyDescriptor(_t, sym) {
    if (typeof sym === 'string' && sym in positionsDerivedStore.byKey) {
      return { configurable: true, enumerable: true, value: positionsDerivedStore.byKey[sym]?.day_pnl ?? 0 };
    }
    return undefined;
  },
});

export const positionsDayPnlStore = {
  get total() { return positionsDerivedStore.total.day_pnl; },
  get byKey() { return _byKeyProxy; },
  setFromPulse() {},
};

// no-op export so old import { setFromPulse } patterns compile
export function setFromPulse() {}
