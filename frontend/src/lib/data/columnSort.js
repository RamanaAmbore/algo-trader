/**
 * columnSort.js — Shared, framework-agnostic 3-state column-sort SSOT for
 * hand-rolled (non-ag-Grid) grids. Mirrors ag-Grid's own click cycle
 * (`sortingOrder: ['asc', 'desc', null]`, see algoGridUtils.js's
 * SORT_CYCLE) so a hand-rolled grid behaves identically to every Pulse
 * grid, not just visually.
 *
 * Pure, plain `.js` (no Svelte reactive state) — Vitest-testable in
 * isolation. `columnSort.svelte.js` wraps this in a `$state`-backed
 * reactive factory for component use.
 *
 * Future hand-rolled grids should reuse this pair rather than building a
 * bespoke sort mechanism (operator: "this functionality is ssot").
 */

import { SORT_CYCLE } from './algoGridUtils.js';

/** @typedef {{ column: string | null, direction: 'asc' | 'desc' | null }} SortState */
/** @typedef {{ type: 'text' | 'number', accessor: (row: any) => any }} ColumnSpec */

/**
 * Advance the 3-state sort cycle for a clicked column.
 *
 * A different column than the currently-sorted one always starts at
 * `SORT_CYCLE[0]` ('asc') — clicking a new header never resumes wherever
 * the previous column's cycle left off. Reaching the cycle's terminal
 * `null` clears `column` too, so an inert (no sort) state is always the
 * canonical `{ column: null, direction: null }` shape, never a column
 * name paired with a null direction.
 *
 * @param {SortState | null | undefined} current
 * @param {string} columnKey
 * @returns {SortState}
 */
export function cycleSortState(current, columnKey) {
  const cur = current || { column: null, direction: null };
  if (cur.column !== columnKey) {
    return { column: columnKey, direction: SORT_CYCLE[0] };
  }
  const idx = SORT_CYCLE.indexOf(cur.direction);
  const nextDir = SORT_CYCLE[(idx + 1) % SORT_CYCLE.length];
  return nextDir == null ? { column: null, direction: null } : { column: columnKey, direction: nextDir };
}

/**
 * Base (ascending, direction-agnostic) comparator. Null/undefined is
 * treated as the MINIMUM value — mirrors ag-Grid's own
 * `_defaultComparator` (confirmed from ag-grid-community.js source), not
 * a fixed "nulls last" rule. `applyColumnSort` multiplies this by ±1 for
 * asc/desc, which is what naturally produces "nulls first on asc, nulls
 * last on desc" without a second branch.
 *
 * @param {any} a
 * @param {any} b
 * @param {'text' | 'number'} type
 * @returns {number}
 */
function _compareAscNullsFirst(a, b, type) {
  const aNull = a == null;
  const bNull = b == null;
  if (aNull && bNull) return 0;
  if (aNull) return -1;
  if (bNull) return 1;
  if (type === 'text') {
    const as = String(a);
    const bs = String(b);
    return as < bs ? -1 : as > bs ? 1 : 0;
  }
  return a < b ? -1 : a > b ? 1 : 0;
}

/**
 * Apply an explicit column sort to `rows`.
 *
 * Returns the SAME array reference, unchanged, when `sortState.direction`
 * is `null` (or `sortState`/the resolved column spec is missing) — this
 * is a hard contract callers rely on to skip evaluating any accessor
 * (e.g. a live-tick LTP/P&L read) on the default (no active sort)
 * render path.
 *
 * Decorate-sort-undecorate: each row's accessor is evaluated exactly
 * once. A `NaN` numeric key is coerced to `null` (keeps the comparator's
 * nulls-first/last rule well-defined; `NaN` comparisons are never
 * transitive). Ties break by the row's ORIGINAL index, ascending —
 * stable regardless of asc/desc, since only the value comparison (not
 * the index tiebreak) is negated for desc.
 *
 * @param {any[]} rows
 * @param {SortState | null | undefined} sortState
 * @param {Record<string, ColumnSpec>} columnSpecs
 * @returns {any[]}
 */
export function applyColumnSort(rows, sortState, columnSpecs) {
  if (!sortState || sortState.direction == null || !sortState.column) return rows;
  const spec = columnSpecs?.[sortState.column];
  if (!spec || typeof spec.accessor !== 'function') return rows;
  const dir = sortState.direction === 'desc' ? -1 : 1;

  const decorated = rows.map((row, idx) => {
    let key = spec.accessor(row);
    if (typeof key === 'number' && Number.isNaN(key)) key = null;
    return { row, key, idx };
  });

  decorated.sort((a, b) => {
    const cmp = _compareAscNullsFirst(a.key, b.key, spec.type) * dir;
    if (cmp !== 0) return cmp;
    return a.idx - b.idx;
  });

  return decorated.map((d) => d.row);
}
