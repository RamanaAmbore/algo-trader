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
  // Real-money fix (2026-10, NavStrip P∆ pill): propagate `null` (not `0`)
  // when the underlying portfolioStore.positions.total.day_pnl is itself
  // `null` — genuinely unknown, no positions poll has ever landed this
  // session. A broker-confirmed-flat book still returns a real `0` once
  // the first live cycle has run (see portfolioStore.svelte.js's
  // _EMPTY_POSITIONS cold-start default + freeze-to-last-good mechanism).
  get total() { return portfolioStore.positions.total.day_pnl; },
  get byKey() { return _byKeyProxy; },
  /** Per-account positions day P&L, keyed by UPPERCASE account + 'TOTAL'. */
  get byAccount() { return portfolioStore.positions.byAccount ?? {}; },
  /**
   * Day P&L total respecting an account filter — sums `byAccount[acct]`
   * for each account in `accounts`, or falls back to the firm-wide
   * `.total` when `accounts` is empty (no filter applied). 2026-09-27
   * audit fix: Pulse's positions TOTAL row previously always read the
   * unfiltered `.total`, disagreeing with the account-filtered grid
   * rows sitting right above it.
   * @param {string[]} accounts
   */
  filteredTotal(accounts) {
    if (!accounts || accounts.length === 0) return this.total;
    const by = this.byAccount;
    let sum = 0;
    for (const a of accounts) sum += Number(by[String(a).toUpperCase()]) || 0;
    return sum;
  },
  setFromPulse() {},
};

// no-op export so old import { setFromPulse } patterns compile
export function setFromPulse() {}
