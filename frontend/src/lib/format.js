/**
 * Shared number-formatting helpers.
 *
 * priceFmt  — live quotes, order/fill prices (2 dp, en-IN)
 * pctFmt    — percentages / ratios (2 dp, caller appends %)
 * aggFmt    — ₹ P&L, margins, cash, position value (0 dp, en-IN)
 * qtyFmt    — share/lot counts (0 dp, en-IN, alias of aggFmt)
 *
 * All helpers return '—' for null / undefined / NaN / ±Infinity.
 * Negative values render with a minus sign: -1,50,000 (not parens).
 */

const _IN2 = new Intl.NumberFormat('en-IN', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const _IN0 = new Intl.NumberFormat('en-IN', {
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

/** 2 dp, Indian grouping — for LTP, avg price, limit, fill, slippage. */
export function priceFmt(v) {
  if (v == null || !isFinite(v)) return '—';
  return _IN2.format(Number(v));
}

/** 2 dp fixed — for percentages and ratios (caller appends % if needed). */
export function pctFmt(v) {
  if (v == null || !isFinite(v)) return '—';
  return Number(v).toFixed(2);
}

/** 0 dp, Indian grouping — for ₹ aggregates (P&L, margins, cash). */
export function aggFmt(v) {
  if (v == null || !isFinite(v)) return '—';
  return _IN0.format(Math.round(Number(v)));
}

/** Alias of aggFmt — for share/lot counts. */
export const qtyFmt = aggFmt;
