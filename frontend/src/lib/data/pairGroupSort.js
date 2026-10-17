/**
 * pairGroupSort.js — Shared lot-based pair/orphan sort helper for all
 * positions grids (MarketPulse, PerformancePage, derivatives cand-grid).
 *
 * Extracted as a standalone module so all four grids share one SSOT.
 */

import { leadAccount } from '$lib/account';

/**
 * Resolve a row's account for sort-key purposes, handling both row
 * shapes in play across the shared callers:
 *   - PerformancePage / derivatives Legs rows: singular `row.account`.
 *   - MarketPulse rows (pulseUnified.js:mergePositionRows): `row.accounts`,
 *     a Set — always exactly one entry post-Part-A (one row per account),
 *     except the synthetic symbol-subtotal row, which has an EMPTY Set
 *     (`leadAccount` returns null there, same as "no account" below).
 * @param {any} row
 * @returns {string}
 */
function _rowAccount(row) {
  if (row?.account != null) return String(row.account);
  return leadAccount(row) ?? '';
}

/**
 * pairGroupSort(rowNodes) — SSOT post-sort for all positions grids.
 *
 * Sort order (2026-10-17, operator: "the positions and legs sort order
 * should be on account and then pairs, unpaired showing the last within
 * the account"): ACCOUNT first, then within each account P1 group → P2
 * → … → that account's own orphan rows last. `account` is the PRIMARY
 * sort segment (not just a tie-breaker) so two different accounts' rows
 * never interleave, and the orphan marker is account-scoped (not a bare
 * global constant) so one account's unpaired rows sort to the end of
 * THAT account's own cluster, not to the very end of the whole grid
 * mixed in with every other account's orphans.
 * Sort key: `${account}|${pair_group_key ?? "ZZZZ_orphan"}`
 * Within each (account, pair_group_key) group: preserve original row
 * order (stable by index).
 *
 * NOTE: callers wiring this to ag-Grid's `postSortRows` callback must
 * wrap it: `postSortRows: (params) => pairGroupSort(params.nodes)`
 *
 * @param {any[]} rowNodes  — ag-Grid IRowNode[] (mutated in-place)
 */
export function pairGroupSort(rowNodes) {
  if (!rowNodes || rowNodes.length === 0) return;
  // Build stable index map first so within-group order is preserved.
  const idx = new Map(rowNodes.map((n, i) => [n, i]));
  const key = (n) => `${_rowAccount(n.data)}|${n.data?.pair_group_key ?? 'ZZZZ_orphan'}`;
  rowNodes.sort((a, b) => {
    const ka = key(a);
    const kb = key(b);
    if (ka !== kb) return ka < kb ? -1 : 1;
    return (idx.get(a) ?? 0) - (idx.get(b) ?? 0); // stable within group
  });
}

/**
 * sortRowsByPairGroup(rows) — pure-array variant of pairGroupSort for
 * contexts that don't go through ag-Grid's postSortRows (e.g. the
 * derivatives Legs grid, a hand-rolled {#each} list over plain row
 * objects, not IRowNode[]). Returns a NEW array; does not mutate the
 * input. Same group-first, stable-within-group semantics as
 * pairGroupSort.
 *
 * Sort key: `${account}|${pair_group_key ?? "ZZZZ_orphan"}` — `account`
 * is ALWAYS the primary segment, for both paired AND orphan rows (2026-
 * 10-17, operator: "the positions and legs sort order should be on
 * account and then pairs, unpaired showing the last within the
 * account"). Previously the orphan branch used a bare global
 * `'ZZZZ_orphan'` constant with no account segment at all — every
 * account's unpaired rows collapsed to the exact same key and clumped
 * together at the very end of the WHOLE list, instead of each
 * account's own orphans sorting to the end of THAT account's own
 * cluster. The derivatives Legs grid can show rows from MULTIPLE
 * accounts at once (the account multi-select filter), and the backend's
 * P1/P2/... numbering is now GLOBALLY sequential (not reset per group —
 * see `_auto_pair_positions`), so two different accounts' pair labels
 * are already guaranteed distinct; the account segment here is what
 * keeps one account's rows from interleaving with another's regardless.
 * Only applied to the `pair_group_key` auto-pairing case (not
 * `pin_group_key`) per the operator's explicit scoping.
 *
 * @param {any[]} rows — plain row objects carrying `account`/`pair_group_key`
 * @returns {any[]}
 */
export function sortRowsByPairGroup(rows) {
  if (!rows || rows.length === 0) return rows ?? [];
  const key = (r) => `${_rowAccount(r)}|${r?.pair_group_key ?? 'ZZZZ_orphan'}`;
  return rows
    .map((r, i) => ({ r, i }))
    .sort((a, b) => {
      const ka = key(a.r);
      const kb = key(b.r);
      if (ka !== kb) return ka < kb ? -1 : 1;
      return a.i - b.i; // stable within group
    })
    .map(x => x.r);
}

