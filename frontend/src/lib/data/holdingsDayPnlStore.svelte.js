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

const _EMPTY_HOLD = Object.freeze({ day_pnl: null, chg_pct: null });

export const holdingsDayPnlStore = {
  get total()        { return portfolioStore.holdings.total ?? 0;       },
  get byKey()        { return portfolioStore.holdings.byKey;            },
  get byAccount()    { return portfolioStore.holdings.byAccount;        },
  get chg_pct()      { return portfolioStore.holdings.chg_pct ?? null;  },
  get chgPctByKey()  { return portfolioStore.holdings.chgPctByKey ?? {}; },

  /**
   * Get holdings day P&L by symbol. Returns _EMPTY_HOLD if not found.
   * @param {string} sym
   * @returns {{ day_pnl: number|null, chg_pct: number|null }}
   */
  get(sym) {
    const sym_upper = String(sym || '').toUpperCase();
    return {
      day_pnl: this.byKey[sym_upper] ?? null,
      chg_pct: this.chgPctByKey[sym_upper] ?? null,
    };
  },

  /**
   * Called by MarketPulse after each buildUnified with cq-accurate per-symbol
   * and aggregate values. Delegates to portfolioStore.setHoldingsFromPulse.
   * @param {Record<string,number>} byKey
   * @param {number} total
   */
  setFromPulse(byKey, total) {
    portfolioStore.setHoldingsFromPulse(byKey, total);
  },
};
