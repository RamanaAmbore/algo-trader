/**
 * Shared ag-Grid base options and formatter helpers.
 *
 * Formatter note: value formatters for currency, percentage, and price are
 * already in `$lib/format` (aggFmtGrid, pctFmtGrid, priceFmt etc.) and all
 * grid consumers import from there. The helpers below are convenience wrappers
 * for grids that need a quick one-off formatter without importing the full
 * format module. Do NOT replace existing $lib/format imports with these — use
 * these only in new grids.
 *
 * Usage:
 *   import { GRID_BASE_OPTS } from '$lib/data/gridDefaults.js';
 *   createGrid(el, { ...GRID_BASE_OPTS, columnDefs, rowData });
 */

/** Shared ag-Grid base options — spread into every createGrid() call */
export const GRID_BASE_OPTS = {
  suppressMovableColumns: true,
  suppressCellFocus: true,
  animateRows: true,
  headerHeight: 28,
};

/**
 * Number formatter factory — dp = decimal places.
 * Prefer aggFmtGrid / priceFmt from $lib/format for existing grids.
 * @param {number} dp
 */
export function fmtNum(dp = 2) {
  return (params) => {
    const v = params.value;
    if (v == null || isNaN(v)) return '—';
    return v.toLocaleString('en-IN', { minimumFractionDigits: dp, maximumFractionDigits: dp });
  };
}

/**
 * Percentage formatter (expects a 0–1 fraction, renders as xx.xx%).
 * Prefer pctFmtGrid from $lib/format for existing grids.
 * @param {{ value: number | null }} params
 */
export function fmtPct(params) {
  const v = params.value;
  if (v == null || isNaN(v)) return '—';
  return `${(v * 100).toFixed(2)}%`;
}

/**
 * Currency formatter (₹) with compact notation.
 * Prefer aggFmtGrid from $lib/format for existing grids.
 * @param {{ value: number | null }} params
 */
export function fmtCcy(params) {
  const v = params.value;
  if (v == null || isNaN(v)) return '—';
  const abs = Math.abs(v);
  const sign = v < 0 ? '−' : '';
  if (abs >= 1e7) return `${sign}₹${(abs / 1e7).toFixed(2)}Cr`;
  if (abs >= 1e5) return `${sign}₹${(abs / 1e5).toFixed(2)}L`;
  return `${sign}₹${abs.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
}
