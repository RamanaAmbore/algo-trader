/**
 * holdingsDayPnlStore — backward-compat shim.
 *
 * All computation has moved to portfolioStore.svelte.js (Phase 1 refactor).
 * This shim re-exports the same API surface so all existing consumers compile
 * without changes.
 *
 * Consumers read:
 *   .total     → number  (pulse-overridable)
 *   .byKey     → { [tradingsymbol]: number }  (pulse-overridable)
 *   .byAccount → { [account]: number, TOTAL: number }  (TOTAL is pulse-aware)
 *
 * setFromPulse(byKey, total) delegates to portfolioStore.setHoldingsFromPulse()
 * so MarketPulse keeps writing to a single SSOT.
 */

import { portfolioStore } from './portfolioStore.svelte.js';

export const holdingsDayPnlStore = {
  get total()        { return portfolioStore.holdings.total ?? 0;       },
  get byKey()        { return portfolioStore.holdings.byKey;            },
  get byAccount()    { return portfolioStore.holdings.byAccount;        },
  get chg_pct()      { return portfolioStore.holdings.chg_pct ?? null;  },
  get chgPctByKey()  { return portfolioStore.holdings.chgPctByKey ?? {}; },

  /**
   * Get holdings day P&L by symbol.
   * @param {string} sym
   * @param {number|null} [fallback=null] value used when field is absent/null
   * @returns {{ day_pnl: number|null, chg_pct: number|null }}
   */
  get(sym, fallback = null) {
    const sym_upper = String(sym || '').toUpperCase();
    return {
      day_pnl: this.byKey[sym_upper] ?? fallback,
      chg_pct: this.chgPctByKey[sym_upper] ?? fallback,
    };
  },

  /**
   * No-op — the MarketPulse pulse-override write has been removed (Fix 3d).
   * Holdings totals are now derived exclusively from portfolioStore.holdings.*
   * (the _holdAgg tier chain). Kept as a no-op so any remaining callers
   * (e.g. tests mocking the store shape) compile without changes.
   * @param {Record<string,number>} _byKey
   * @param {number} _total
   */
  // eslint-disable-next-line no-unused-vars
  setFromPulse(_byKey, _total) {},
};
