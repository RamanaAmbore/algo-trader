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

/** Live quotes / order prices.
 *  |v| < 500 → 2 decimals  (option premiums, slippage, sub-rupee tickers)
 *  |v| ≥ 500 → 0 decimals  (NIFTY ~22k, gold, futures — decimals are noise
 *                           at that magnitude and steal column width).
 *  Examples: 0.05 → "0.05", 1234.5 → "1,235", 22156.7 → "22,157" */
export function priceFmt(v) {
  if (v == null || !isFinite(v)) return '—';
  const n = Number(v);
  return Math.abs(n) >= 500 ? _IN0.format(Math.round(n)) : _IN2.format(n);
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
