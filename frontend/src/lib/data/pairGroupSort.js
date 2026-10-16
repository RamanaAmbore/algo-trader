/**
 * pairGroupSort.js — Shared lot-based pair/orphan sort helper for all
 * positions grids (MarketPulse, PerformancePage, derivatives cand-grid).
 *
 * Extracted as a standalone module so all four grids share one SSOT.
 */

/**
 * pairGroupSort(rowNodes) — SSOT post-sort for all positions grids.
 *
 * Sort order: P1 group → P2 → … → orphan group last.
 * Sort key: data.pair_group_key ?? "ZZZZ_orphan"
 * Within each group: preserve original row order (stable by index).
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
  rowNodes.sort((a, b) => {
    const ka = a.data?.pair_group_key ?? 'ZZZZ_orphan';
    const kb = b.data?.pair_group_key ?? 'ZZZZ_orphan';
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
 * Sort key: `${account}|${pair_group_key}` — NOT the bare
 * `pair_group_key` alone. The derivatives Legs grid can show rows from
 * MULTIPLE accounts at once (the account multi-select filter), and the
 * backend's P1/P2 numbering is scoped per-account — two different
 * accounts' unrelated "P1" legs would otherwise interleave as if they
 * were one group. Harmless if pair_group_key values turn out to already
 * be globally unique; this is a defensive composite key either way. Only
 * applied to the `pair_group_key` auto-pairing case (not `pin_group_key`)
 * per the operator's explicit scoping.
 *
 * @param {any[]} rows — plain row objects carrying `account`/`pair_group_key`
 * @returns {any[]}
 */
export function sortRowsByPairGroup(rows) {
  if (!rows || rows.length === 0) return rows ?? [];
  return rows
    .map((r, i) => ({ r, i }))
    .sort((a, b) => {
      const ka = a.r?.pair_group_key != null
        ? `${a.r?.account ?? ''}|${a.r.pair_group_key}`
        : 'ZZZZ_orphan';
      const kb = b.r?.pair_group_key != null
        ? `${b.r?.account ?? ''}|${b.r.pair_group_key}`
        : 'ZZZZ_orphan';
      if (ka !== kb) return ka < kb ? -1 : 1;
      return a.i - b.i; // stable within group
    })
    .map(x => x.r);
}

