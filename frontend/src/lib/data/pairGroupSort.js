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
 * pairChipHtml(data) — Returns HTML string for pair/orphan chips.
 *
 * Renders:
 *   - A `.pair-chip` span when `data.pair_group_key` is set.
 *   - An `.orphan-chip` span when `orphan_qty > 0 && paired_qty > 0`
 *     (mixed row: some lots are paired, some are orphaned).
 *
 * Returns '' when neither condition is met (watchlist / holdings rows,
 * positions without a pair key, or pure orphan rows that don't carry
 * a split).
 *
 * @param {any} data  — ag-Grid row data object (params.data)
 * @returns {string}
 */
export function pairChipHtml(data) {
  if (!data) return '';
  const parts = [];
  if (data.pair_group_key) {
    parts.push(`<span class="pair-chip">${data.pair_group_key}</span>`);
  }
  if (data.orphan_qty > 0 && data.paired_qty > 0) {
    parts.push(`<span class="orphan-chip">${data.orphan_qty}L orphan</span>`);
  }
  return parts.join(' ');
}
