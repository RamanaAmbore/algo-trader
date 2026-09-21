/**
 * positionsDayPnlStore — backward-compat shim.
 * Delegates to portfolioStore; exposes the legacy numeric API so
 * existing consumers (NavCard, NavBreakdown, MarketPulse) need no changes.
 *
 *   .total         → number  (= portfolioStore.positions.total.day_pnl)
 *   .byKey[sym]    → number  (= portfolioStore.positions.byKey[sym]?.day_pnl ?? 0)
 *   .setFromPulse  → no-op
 *
 * Also re-exports holdingsDayPnlStore for consumers that imported both from
 * this module (positionsDayPnlStore was the original combined entry point).
 */
import { portfolioStore } from '$lib/data/portfolioStore.svelte.js';
export { holdingsDayPnlStore } from '$lib/data/holdingsDayPnlStore.svelte.js';

const _byKeyProxy = new Proxy({}, {
  get(_t, sym) {
    if (typeof sym !== 'string') return undefined;
    return portfolioStore.positions.byKey[sym]?.day_pnl ?? 0;
  },
  has(_t, sym) { return sym in portfolioStore.positions.byKey; },
  // ownKeys + getOwnPropertyDescriptor required so Object.entries/keys work.
  // Without these, Object.entries returns [] (target is {}) — breaking
  // _fnoDayPnlByRoot in the derivatives page which iterates byKey.
  ownKeys(_t) { return Object.keys(portfolioStore.positions.byKey); },
  getOwnPropertyDescriptor(_t, sym) {
    if (typeof sym === 'string' && sym in portfolioStore.positions.byKey) {
      return { configurable: true, enumerable: true, value: portfolioStore.positions.byKey[sym]?.day_pnl ?? 0 };
    }
    return undefined;
  },
});

export const positionsDayPnlStore = {
  get total() { return portfolioStore.positions.total.day_pnl ?? 0; },
  get byKey() { return _byKeyProxy; },
  /** Per-account positions day P&L, keyed by UPPERCASE account + 'TOTAL'. */
  get byAccount() { return portfolioStore.positions.byAccount ?? {}; },
  setFromPulse() {},
};

// no-op export so old import { setFromPulse } patterns compile
export function setFromPulse() {}
