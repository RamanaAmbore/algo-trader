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
 *
 * 2026-09 audit: this default (amber-for-negative) does NOT match
 * PositionStrip's actual `.ps-neg` convention (red, var(--c-short)) for
 * genuine P&L columns — it was a NavBreakdown-only choice that got
 * over-applied to Day P&L / Lifetime P&L (see mkDirCellText below, which
 * NavBreakdown's Day/Lifetime P&L columns now use instead). This default
 * remains correct/unchanged for every OTHER caller (margin, cash,
 * holdings value, and NavBreakdown's Expiry P&L column, which is
 * genuinely amber-flavoured regardless of sign on PositionStrip's own
 * `.ps-exp` pill) — none of those were asked to change in this pass.
 *   positive → --algo-green, negative → --algo-amber, zero/neutral → --algo-dim (muted;
 *   app.css's .dir-flat rule — kept in sync with pnl-zero's identical muted-zero choice)
 * @param {import('ag-grid-community').CellClassParams} p
 * @returns {string}
 */
export const agDirCellText = (p) => {
  const v = p.value ?? 0;
  return `ag-right-aligned-cell ${v > 0 ? 'dir-gain' : v < 0 ? 'dir-loss' : 'dir-flat'}`;
};

/**
 * cellClass factory — parameterised variant of agDirCellText. Default
 * (no options / `{}`) is byte-identical behaviour to agDirCellText
 * (amber-for-negative) — every existing caller is unaffected. Pass
 * `{ lossRed: true }` for genuine P&L columns where negative should read
 * as a LOSS (red, var(--c-short) via the `dir-loss-red` class) — matching
 * PositionStrip's `.ps-neg` convention (A6, 2026-09 audit: NavBreakdown's
 * Day P&L / Lifetime P&L columns were wrongly using the amber-for-
 * negative default, a misapplication of the Expiry P&L column's
 * genuinely-flat-amber-regardless-of-sign exception to columns that
 * should behave like every other P&L surface).
 * @param {{ lossRed?: boolean }} [opts]
 * @returns {(p: import('ag-grid-community').CellClassParams) => string}
 */
export function mkDirCellText(opts = {}) {
  const lossClass = opts.lossRed ? 'dir-loss-red' : 'dir-loss';
  return (p) => {
    const v = p.value ?? 0;
    return `ag-right-aligned-cell ${v > 0 ? 'dir-gain' : v < 0 ? lossClass : 'dir-flat'}`;
  };
}

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
 *   - rowHeight: 28 (desktop) / 36 (mobile) — must match --ag-row-height CSS var.
 *     Callers MUST also set el.style.setProperty('--ag-row-height', `${rowH}px`) on
 *     the grid container element to keep JS rowHeight and CSS centering var in sync.
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
    rowHeight: _isMobile ? 36 : 28,
    getRowId: ({ data }) => {
      if (!data) return '';
      if (data.symbol)  return String(data.symbol);
      if (data.account) return String(data.account);
      return '';
    },
    ...overrides,
  };
}

/**
 * syncGridRowHeightVar — sets the --ag-row-height CSS custom property on
 * a grid container element to match the JS rowHeight. Call this immediately
 * after createGrid() to ensure ag-Grid's legacy theme line-height derivation
 * (used for cell text vertical centering) stays in sync with the actual row height.
 *
 * Usage:
 *   const grid = createGrid(el, mkBaseGridOpts());
 *   syncGridRowHeightVar(el);
 *
 * @param {HTMLElement} gridEl - The grid container element (passed to createGrid)
 */
export function syncGridRowHeightVar(gridEl) {
  const rowH = _isMobile ? 36 : 28;
  gridEl.style.setProperty('--ag-row-height', `${rowH}px`);
}
