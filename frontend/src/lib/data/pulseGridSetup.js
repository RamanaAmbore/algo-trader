/**
 * pulseGridSetup.js — Pure ag-Grid config shared by MarketPulse.
 *
 * Extracted from MarketPulse.svelte (Phase 1.5 refactor). Everything
 * here is a plain object or a function that reads only its own
 * parameters — no closure over Svelte reactive state.
 *
 * Consumers: MarketPulse.svelte (via import).
 */

/**
 * Shared defaultColDef for every Pulse ag-Grid instance.
 * All four grids (bucket × 2 + summary × 2 + funds) use this
 * identical shape; a single reference removes four copies.
 */
export const PULSE_DEFAULT_COL_DEF = /** @type {import('ag-grid-community').ColDef} */ ({
  resizable: true,
  sortable: true,
  suppressMovable: true,
  suppressHeaderMenuButton: true,
});

/**
 * Canonical column-sort cycle for all Pulse grids.
 * asc → desc → unsorted (null restores the natural row order).
 */
export const PULSE_SORTING_ORDER = /** @type {('asc'|'desc'|null)[]} */ (
  ['asc', 'desc', null]
);

/**
 * Row-id function for the six bucket grids (Pinned, Watch, Positions,
 * Holdings, Winners, Losers). Uses the row's stable composite key
 * when present; falls back to tradingsymbol.
 *
 * @param {{ data: any }} params
 * @returns {string}
 */
export function pulseRowId({ data }) {
  return String(data?.key || data?.tradingsymbol || '');
}

/**
 * Row-id function for the three auxiliary grids (Positions Summary,
 * Holdings Summary, Funds). Keyed by broker account id.
 *
 * @param {{ data: any }} params
 * @returns {string}
 */
export function summaryRowId({ data }) {
  return String(data?.account || '');
}

/**
 * ag-Grid postSortRows callback — keeps an option/future and its
 * underlying as one contiguous block after the user sorts a column.
 *
 * Algorithm:
 *   1. Walk post-sort rows; cluster by `data.underlying` name.
 *   2. Rows without an underlying (cash equity, watchlist items
 *      with no F&O coverage) stay at their own sort position.
 *   3. The first occurrence of an underlying group anchors that
 *      cluster's position; subsequent group members are attached
 *      immediately after the anchor.
 *   4. Mutates the `nodes` array in place (ag-Grid contract).
 *
 * Pure: reads only the `nodes` param — no closure over reactive state.
 *
 * @param {{ nodes: import('ag-grid-community').RowNode[] }} param
 */
export function postSortGroups({ nodes }) {
  if (!nodes || nodes.length === 0) return;

  /** @type {Map<string, import('ag-grid-community').RowNode[]>} */
  const byKey = new Map();
  /** @type {string[]} */
  const orderedGroupKeys = [];
  /** @type {import('ag-grid-community').RowNode[]} */
  const standaloneOrder = [];

  for (let i = 0; i < nodes.length; i++) {
    const n = nodes[i];
    const d = n.data || {};
    // Group key: underlying name.  Rows without an underlying get no
    // key so they retain their individual sort positions.
    const u = String(d.underlying || '').toUpperCase();
    if (!u) {
      standaloneOrder.push(n);
      continue;
    }
    if (!byKey.has(u)) {
      byKey.set(u, []);
      orderedGroupKeys.push(u);
    }
    byKey.get(u).push(n);
  }

  // Interleave groups + standalone rows by first-appearance in the
  // sorted list so the overall sort order is respected between groups.
  /** @type {Map<string, number>} */
  const firstIdxOf = new Map();
  for (const k of orderedGroupKeys) {
    firstIdxOf.set(k, nodes.indexOf(byKey.get(k)[0]));
  }

  /** @type {Array<{first:number, kind:'g'|'s', key?:string, node?: import('ag-grid-community').RowNode}>} */
  const seq = [];
  for (const k of orderedGroupKeys) {
    seq.push({ first: firstIdxOf.get(k), kind: 'g', key: k });
  }
  for (const n of standaloneOrder) {
    seq.push({ first: nodes.indexOf(n), kind: 's', node: n });
  }
  seq.sort((a, b) => a.first - b.first);

  // Reassemble in-place (ag-Grid postSortRows contract).
  const out = [];
  for (const entry of seq) {
    if (entry.kind === 'g') {
      for (const n of byKey.get(entry.key)) out.push(n);
    } else {
      out.push(entry.node);
    }
  }
  nodes.length = 0;
  for (const n of out) nodes.push(n);
}
