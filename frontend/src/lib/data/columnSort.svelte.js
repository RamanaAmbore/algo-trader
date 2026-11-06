/**
 * columnSort.svelte.js — Thin `$state`-backed reactive wrapper around
 * columnSort.js's pure `cycleSortState`. Mirrors the tickFlash.svelte.js
 * split (plain `.js` core + reactive `.svelte.js` wrapper) so the core
 * cycle logic stays Vitest-testable while components get a single
 * reactive handle.
 *
 * `createColumnSort()` itself cannot be INVOKED under Vitest (no Svelte
 * rune compiler in this project's Vitest toolchain — same documented
 * constraint as every other `*.svelte.js` module here, e.g.
 * symbolStorePersist.test.js's own header comment; importing this file
 * is safe, calling the factory throws). `columnSort.test.js` covers
 * `cycleSortState`/`applyColumnSort` directly; this wrapper is a thin,
 * inspectable delegation with nothing else to regress.
 *
 * Usage (derivatives Legs/Candidates grid, or any future hand-rolled
 * grid wanting Pulse-grid-style column sort):
 *
 *   const candSort = createColumnSort();
 *   // header click:
 *   candSort.toggle('ltp');
 *   // header markup:
 *   <span aria-sort={candSort.ariaSort('ltp')}>LTP {candSort.glyph('ltp')}</span>
 */

import { cycleSortState } from './columnSort.js';

/**
 * @returns {{
 *   readonly column: string | null,
 *   readonly direction: 'asc' | 'desc' | null,
 *   toggle: (columnKey: string) => void,
 *   ariaSort: (columnKey: string) => 'ascending' | 'descending' | 'none',
 *   glyph: (columnKey: string) => '▲' | '▼' | null,
 * }}
 */
export function createColumnSort() {
  let column = $state(/** @type {string | null} */ (null));
  let direction = $state(/** @type {'asc' | 'desc' | null} */ (null));

  return {
    get column() { return column; },
    get direction() { return direction; },

    /**
     * Advance the 3-state cycle for the clicked column (asc → desc →
     * unsorted). See columnSort.js:cycleSortState for the exact rules.
     * @param {string} columnKey
     */
    toggle(columnKey) {
      const next = cycleSortState({ column, direction }, columnKey);
      column = next.column;
      direction = next.direction;
    },

    /**
     * ARIA `aria-sort` value for a header cell.
     * @param {string} columnKey
     * @returns {'ascending' | 'descending' | 'none'}
     */
    ariaSort(columnKey) {
      if (column !== columnKey || direction == null) return 'none';
      return direction === 'asc' ? 'ascending' : 'descending';
    },

    /**
     * Sort-direction glyph for a header cell, or `null` when this column
     * isn't the active sort. Callers should render the glyph span
     * UNCONDITIONALLY and toggle `visibility: hidden/visible` on this
     * value (never `display: none`) to avoid column-width reflow — see
     * the derivatives grid header CSS for the convention.
     * @param {string} columnKey
     * @returns {'▲' | '▼' | null}
     */
    glyph(columnKey) {
      if (column !== columnKey) return null;
      if (direction === 'asc') return '▲';
      if (direction === 'desc') return '▼';
      return null;
    },
  };
}
