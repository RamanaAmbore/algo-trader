/**
 * algoGridUtils.js — shared ag-Grid helpers for the algo dark theme.
 *
 * Centralises column header class, value formatters, direction cellClass,
 * and the common base grid options so every algo-palette grid (dashboard,
 * NavBreakdown, derivatives, etc.) reads from one source and stays
 * visually consistent.
 *
 * Usage:
 *   import { NUMERIC_HDR, agNumFmt, agAggFmt, agPctFmt, agDirCell, agDirCellText, mkBaseGridOpts }
 *     from '$lib/data/algoGridUtils.js';
 */

import { priceFmt, pctFmt, aggCompact } from '$lib/format';

/** True when the viewport is narrow (≤720px) at module evaluation time.
 *  Used to set rowHeight so CSS and JS stay in sync on mobile. */
const _isMobile = typeof window !== 'undefined' && window.innerWidth <= 720;

/** Header class for right-aligned numeric columns. */
export const NUMERIC_HDR = 'ag-right-aligned-header';

/**
 * valueFormatter — en-IN price format (₹X,XX,XXX.XX).
 * '—' for null / undefined.
 * @param {{ value: any }} p
 */
export const agNumFmt = ({ value }) =>
  value == null ? '—' : priceFmt(value);

/**
 * valueFormatter — compact aggregate format (1.23L, 45.6K, etc.).
 * '—' for null / undefined.
 * @param {{ value: any }} p
 */
export const agAggFmt = ({ value }) =>
  value == null ? '—' : aggCompact(value);

/**
 * valueFormatter — percentage with a '%' suffix.
 * '—' for null / undefined.
 * @param {{ value: any }} p
 */
export const agPctFmt = ({ value }) =>
  value == null ? '—' : `${pctFmt(value)}%`;

/**
 * cellClass factory — direction-coloured numeric cell.
 * Returns the ag-Grid right-align class + the algo theme's
 * pnl-gain / pnl-loss / pnl-zero colour class.
 * @param {import('ag-grid-community').CellClassParams} p
 */
export const agDirCell = (p) =>
  `ag-right-aligned-cell ${p.value > 0 ? 'pnl-gain' : p.value < 0 ? 'pnl-loss' : 'pnl-zero'}`;

/**
 * cellClass factory — direction-coloured text only (no background tint).
 * Uses dir-gain / dir-loss / dir-flat CSS classes (text-only with !important,
 * overriding the base .ag-cell !important rule by last-declaration order).
 * Color convention matches NavStrip pill values:
 *   positive → --algo-green, negative → --algo-amber, zero/neutral → --algo-slate
 * @param {import('ag-grid-community').CellClassParams} p
 * @returns {string}
 */
export const agDirCellText = (p) => {
  const v = p.value ?? 0;
  return `ag-right-aligned-cell ${v > 0 ? 'dir-gain' : v < 0 ? 'dir-loss' : 'dir-flat'}`;
};

/**
 * mkBaseGridOpts — returns a fresh base config object for every ag-Grid
 * instance on the algo dark palette pages. Merges in `overrides` on top
 * so callers can extend without spreading the whole object manually.
 *
 * Includes:
 *   - theme: 'legacy' (required for ag-theme-quartz + ag-theme-algo class combo)
 *   - defaultColDef: resizable, sortable, non-movable, no header menu
 *   - sortingOrder: ['asc', 'desc', null] — matches the historical dashboard
 *     _baseGridOpts so W/L grid column sorts don't regress
 *   - rowHeight: 26 — matches --ag-header-height: 28px (algo theme)
 *   - getRowId: symbol → account → '' (for in-place rowData updates)
 *
 * @param {object} [overrides]
 * @returns {object}
 */
export function mkBaseGridOpts(overrides = {}) {
  return {
    theme: 'legacy',
    defaultColDef: {
      resizable: true,
      sortable: true,
      suppressMovable: true,
      suppressHeaderMenuButton: true,
    },
    sortingOrder: /** @type {('asc'|'desc'|null)[]} */ (['asc', 'desc', null]),
    rowHeight: _isMobile ? 36 : 26,
    getRowId: ({ data }) => {
      if (!data) return '';
      if (data.symbol)  return String(data.symbol);
      if (data.account) return String(data.account);
      return '';
    },
    ...overrides,
  };
}
