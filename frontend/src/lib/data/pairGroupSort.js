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

