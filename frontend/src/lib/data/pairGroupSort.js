/**
 * pairGroupSort.js — Shared lot-based pair/orphan sort helper for all
 * positions grids (MarketPulse, PerformancePage, derivatives cand-grid).
 *
 * Extracted as a standalone module so all four grids share one SSOT.
 */

import { leadAccount } from '$lib/account';
import { decomposeSymbol } from '$lib/data/decomposeSymbol';

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
 * Resolve a row's tier for sort-key purposes — underlying/future (and
 * spot/equity/anything unparseable) is tier 0, options are tier 1
 * (2026-10-17, operator: "underlying should come at the top of the
 * options" — gold/an underlying future was rendering BELOW its own CE/PE
 * legs). Derived from the symbol TEXT via the shared `decomposeSymbol()`
 * parser rather than a per-row `kind` field, because the derivatives
 * Legs/Exp-Close candidate rows don't reliably carry one (MarketPulse
 * rows do, via pulseUnified.js's own `kind` field, but this keeps ONE
 * code path working for both row shapes). Row shapes differ: MarketPulse
 * uses `tradingsymbol`, Legs/Exp-Close candidates use `symbol`.
 * @param {any} row
 * @returns {0 | 1}
 */
function _rowTier(row) {
  const sym = String(row?.tradingsymbol ?? row?.symbol ?? '');
  return decomposeSymbol(sym).kind === 'opt' ? 1 : 0;
}

/**
 * pairGroupOrderKey(row) — the tier+pair segment of the shared sort key,
 * WITHOUT the account segment (callers compose account separately, as
 * the primary segment, per the 2026-10-17 account-first fix). Exported so
 * `derivativesMath.js`'s Exp Close `bandRowComparator` can reuse the same
 * ordering inside each band, instead of duplicating tier/pair logic.
 * @param {any} row
 * @returns {string}
 */
export function pairGroupOrderKey(row) {
  return `${_rowTier(row)}|${row?.pair_group_key ?? 'ZZZZ_orphan'}`;
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
 * Sort key: `${account}|${tier}|${pair_group_key ?? "ZZZZ_orphan"}` — the
 * `tier` segment (2026-10-17 fix, `pairGroupOrderKey()` above) puts the
 * underlying/future ahead of its own options WITHIN an account's
 * cluster, before pair_group_key governs ordering among the options
 * themselves. Previously this key had no tier segment, so a lone
 * future/underlying row (which usually has no pair_group_key, since
 * futures only waterfall-pair against other futures) sorted into
 * "ZZZZ_orphan" — the END of its account's cluster — even though it
 * should visually anchor the TOP.
 * Within each (account, tier, pair_group_key) group: preserve original
 * row order (stable by index).
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
  const key = (n) => `${_rowAccount(n.data)}|${pairGroupOrderKey(n.data)}`;
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
 * Sort key: `${account}|${tier}|${pair_group_key ?? "ZZZZ_orphan"}` —
 * `account` is ALWAYS the primary segment, for both paired AND orphan
 * rows (2026-10-17, operator: "the positions and legs sort order should
 * be on account and then pairs, unpaired showing the last within the
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
 * The `tier` segment (same-day follow-up fix, `pairGroupOrderKey()`
 * above) puts a lone future/underlying row ahead of its own options
 * within that account's cluster, instead of it falling into
 * "ZZZZ_orphan" and sorting last.
 * Only applied to the `pair_group_key` auto-pairing case (not
 * `pin_group_key`) per the operator's explicit scoping.
 *
 * @param {any[]} rows — plain row objects carrying `account`/`pair_group_key`
 * @returns {any[]}
 */
export function sortRowsByPairGroup(rows) {
  if (!rows || rows.length === 0) return rows ?? [];
  const key = (r) => `${_rowAccount(r)}|${pairGroupOrderKey(r)}`;
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

