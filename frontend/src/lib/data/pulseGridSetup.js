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
 * Pure: reads only the `nodes` and `api` params — no closure over reactive state.
 *
 * @param {{ nodes: import('ag-grid-community').IRowNode[], api: import('ag-grid-community').GridApi }} param
 */
export function postSortGroups({ nodes, api }) {
  // Respect user's column sort — skip underlying grouping when a sort is active.
  if (api?.getColumnState().some(col => col.sort != null)) return;
  if (!nodes || nodes.length === 0) return;

  /** @type {Map<string, import('ag-grid-community').IRowNode[]>} */
  const byKey = new Map();
  /** @type {string[]} */
  const orderedGroupKeys = [];
  /** @type {import('ag-grid-community').IRowNode[]} */
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

  /** @type {Array<{first:number, kind:'g'|'s', key?:string, node?: import('ag-grid-community').IRowNode}>} */
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

/**
 * ag-Grid postSortRows callback for the Positions Breakdown grid —
 * two-level sort: groups rows by `data.underlying`, then within each
 * group orders by instrument type (FUT/EQ first, then CE, then PE)
 * and by expiry+strike ascending.
 *
 * Unlike `postSortGroups` (which preserves the intra-group order from
 * the user's column sort), this variant always enforces a canonical
 * derivative ladder inside each underlying cluster so F&O legs are
 * easy to scan. Standalone rows (no underlying) retain their first-
 * appearance position relative to the groups.
 *
 * Pure: reads only the `nodes` and `api` params — no closure over
 * reactive state.
 *
 * @param {{ nodes: import('ag-grid-community').IRowNode[], api: import('ag-grid-community').GridApi }} param
 */
export function postSortGroups2Level({ nodes, api }) {
  // Respect user column sort — skip grouping when a sort is active.
  if (api?.getColumnState().some(col => col.sort != null)) return;
  if (!nodes || nodes.length === 0) return;

  const byUnderlying = new Map();
  const underlyingOrder = [];
  const standalone = [];

  for (const n of nodes) {
    const d = n.data || {};
    const u = String(d.underlying || '').toUpperCase();
    if (!u) { standalone.push(n); continue; }
    if (!byUnderlying.has(u)) { byUnderlying.set(u, []); underlyingOrder.push(u); }
    byUnderlying.get(u).push(n);
  }

  // Month-name → zero-padded-number map for chronological expiry sort.
  // Kite monthly tokens: YYMM3L (e.g. "25MAY", "25JUN").  Plain string
  // comparison puts AUG before JAN alphabetically — unusable.
  const _MON = { JAN:'01',FEB:'02',MAR:'03',APR:'04',MAY:'05',JUN:'06',
                 JUL:'07',AUG:'08',SEP:'09',OCT:'10',NOV:'11',DEC:'12' };

  function bucketOrder(n) {
    const t = String((n.data || {}).opt_type || '').toUpperCase();
    if (t === 'CE') return 1;
    if (t === 'PE') return 2;
    return 0; // FUT/EQ first
  }
  function sortKey(n) {
    const d = n.data || {};
    // Normalise expiry: "25MAY" → "2505", "25JUN" → "2506",
    // ISO dates ("2025-05-29") and unknown formats pass through as-is.
    const raw = String(d.expiry || '');
    const m = /^(\d{2})([A-Z]{3})/.exec(raw);
    const expSort = m ? `${m[1]}${_MON[m[2]] || m[2]}` : raw;
    return `${expSort}|${String(d.strike || 0).padStart(10, '0')}`;
  }

  const firstIdx = new Map();
  for (const u of underlyingOrder) firstIdx.set(u, nodes.indexOf(byUnderlying.get(u)[0]));

  const seq = [];
  for (const u of underlyingOrder) seq.push({ first: firstIdx.get(u), kind: 'g', key: u });
  for (const n of standalone) seq.push({ first: nodes.indexOf(n), kind: 's', node: n });
  seq.sort((a, b) => a.first - b.first);

  const out = [];
  for (const entry of seq) {
    if (entry.kind === 'g') {
      const rows = byUnderlying.get(entry.key).slice().sort((a, b) => {
        const bo = bucketOrder(a) - bucketOrder(b);
        if (bo !== 0) return bo;
        const ka = sortKey(a), kb = sortKey(b);
        return ka < kb ? -1 : ka > kb ? 1 : 0;
      });
      out.push(...rows);
    } else {
      out.push(entry.node);
    }
  }
  nodes.length = 0;
  for (const n of out) nodes.push(n);
}
