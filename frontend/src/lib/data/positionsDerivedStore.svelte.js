/**
 * positionsDerivedStore — backward-compat shim.
 *
 * All computation has moved to portfolioStore.svelte.js (Phase 1 refactor).
 * This shim re-exports the same API surface so all existing consumers compile
 * without changes.
 *
 * Consumers that read:
 *   .total           → { day_pnl, exp_pnl, extrinsic }
 *   .expiryTotal     → number (= total.exp_pnl)
 *   .byKey           → { [sym]: { day_pnl, exp_pnl, extrinsic, pnl, prev_mv, chg_pct } }
 *   .expiryByAcct    → Map<account, expiry P&L>
 *   .byRootPositions → { [root]: { day_pnl, exp_pnl, extrinsic, pnl } }
 *   .byRootHoldings  → { [root]: { day_pnl, exp_pnl, extrinsic, pnl } }  (cross-hedge map)
 */

import { portfolioStore } from './portfolioStore.svelte.js';

export const positionsDerivedStore = {
  /** { day_pnl, exp_pnl, extrinsic } — aggregate totals */
  get total()           { return portfolioStore.positions.total;              },
  /** Alias — same as total.exp_pnl, for consumers that used the old expiryTotal */
  get expiryTotal()     { return portfolioStore.positions.total.exp_pnl;      },
  /** { [sym]: { day_pnl, exp_pnl, extrinsic, pnl } } */
  get byKey()           { return portfolioStore.positions.byKey;              },
  /** Map<account, expiry P&L> — for NavBreakdown P slot */
  get expiryByAcct()    { return portfolioStore.positions.expiryByAcct;       },
  /** { [root]: { day_pnl, exp_pnl, extrinsic, pnl } } — for Snapshot */
  get byRootPositions() { return portfolioStore.positions.byRootPositions;    },
  /**
   * { [root]: { exp_pnl, pnl, ... } } — cross-hedge attribution map.
   * Built from the holdings loop in _computeDerived; used by the Snapshot
   * Hold toggle in /admin/derivatives. Shape: { [target_root]: { day_pnl,
   * exp_pnl, extrinsic, pnl } } — NOT the same as holdings.byKey scalars.
   */
  get byRootHoldings()  { return portfolioStore.positions.byRootHoldings;    },

  // no-op: MarketPulse used to override day P&L via this. Now the store is
  // the sole SSOT — Pulse no longer needs to push overrides.
  setFromPulse() {},
};
