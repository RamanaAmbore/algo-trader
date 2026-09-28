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
 *
 * `.byRootPositions`/`.byRootHoldings`/`.getByRoot()` removed (2026-09
 * Commit 9) — grepped every consumer across `frontend/src`; none existed
 * beyond this shim's own definitions and stale comments describing WHERE
 * they used to be read from (already superseded by other SSOT reads —
 * e.g. Snapshot's Exp P&L reduction now goes through
 * `portfolioStore.positions.expPnlRows`, not this map). The underlying
 * `portfolioStore.positions.byRootPositions`/`.byRootHoldings` fields
 * themselves are left in place (out of this cleanup's scope — no proof
 * they're unreachable from portfolioStore's OWN internals, only that
 * this shim's re-export of them had no external readers).
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
  /**
   * Per-row F&O Exp P&L/Extrinsic — one entry per raw position row:
   * { account, symbol, root, source, exp_pnl, extrinsic }. The SSOT the
   * derivatives Snapshot grid's account-filtered totals already read
   * from (`portfolioStore.positions.expPnlRows`) — exposed here too so
   * account-filtered consumers (e.g. MarketPulse's own filtered
   * per-symbol map) can build a lookup scoped to whichever accounts
   * they currently have selected, instead of `.byKey[sym]`'s firm-wide
   * cross-account sum (2026-09-27 audit fix).
   */
  get expPnlRows()      { return portfolioStore.positions.expPnlRows ?? [];   },

  /**
   * Get derived position by symbol.
   * @param {string} sym
   * @param {number|null} [fallback=null] value used for each absent/null field
   * @returns {{ day_pnl: number|null, pnl: number|null, exp_pnl: number|null, extrinsic: number|null, prev_mv: number|null, chg_pct: number|null }}
   */
  get(sym, fallback = null) {
    const r = this.byKey[String(sym || '').toUpperCase()];
    if (!r) return { day_pnl: fallback, pnl: fallback, exp_pnl: fallback, extrinsic: fallback, prev_mv: fallback, chg_pct: fallback };
    return {
      day_pnl:   r.day_pnl   ?? fallback,
      pnl:       r.pnl       ?? fallback,
      exp_pnl:   r.exp_pnl   ?? fallback,
      extrinsic: r.extrinsic ?? fallback,
      prev_mv:   r.prev_mv   ?? fallback,
      chg_pct:   r.chg_pct   ?? fallback,
    };
  },

  // no-op: MarketPulse used to override day P&L via this. Now the store is
  // the sole SSOT — Pulse no longer needs to push overrides.
  setFromPulse() {},
};
