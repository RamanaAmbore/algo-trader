/**
 * Shared number-formatting helpers — the single source of truth for
 * how every grid / strip / log surface displays numbers across the app.
 *
 * Rules baked into the helpers:
 *   - No `₹` prefix anywhere (eats column width; the column header /
 *     chip context already conveys "this is a rupee value").
 *   - No `+` prefix on positives (color coding carries direction).
 *   - Negatives render with a leading `-`.
 *   - Indian-style en-IN grouping (1,50,432 not 150,432).
 *   - Returns '—' for null / undefined / NaN / ±Infinity.
 *
 * The single decimal-presentation rule (used by every helper that can
 * render a fractional value):
 *   |v| <  100 → 2 decimals  (option premiums, sub-rupee tickers,
 *                             percentages, slippage)
 *   |v| ≥  100 → 0 decimals  (NIFTY ~22k, equities, P&L aggregates —
 *                             decimals are noise at that magnitude and
 *                             steal column width)
 *
 * Helpers:
 *   priceFmt    — per-share prices (LTP, avg, fill, slippage)
 *   pctFmt      — percentages / ratios (caller appends '%')
 *   aggFmt      — ₹ aggregates (P&L, margins, cash) when columns aren't tight
 *   aggCompact  — Indian-scale compact ₹ aggregates for tight columns.
 *                 < 1,000 → decimal-rule; < 1,00,000 → "K"; ≥ 1,00,000 → "X.XXL".
 *                 K/L suffix logic is intentionally separate from the
 *                 decimal rule.
 *   qtyFmt      — share/lot counts (always integer — shares are whole)
 *
 * To change the K/L thresholds, the decimal threshold, or any prefix
 * rule — edit this file. Do NOT add `₹` / `+` / lakh-conversion logic
 * at call sites; the call sites should consume these helpers verbatim.
 */

const _IN2 = new Intl.NumberFormat('en-IN', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const _IN0 = new Intl.NumberFormat('en-IN', {
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

function _decFmt(v) {
  if (v == null || !isFinite(v)) return '—';
  const n = Number(v);
  return Math.abs(n) >= 100 ? _IN0.format(Math.round(n)) : _IN2.format(n);
}

export const priceFmt = _decFmt;
export const pctFmt   = _decFmt;
export const aggFmt   = _decFmt;

export function aggCompact(v) {
  if (v == null || !isFinite(v)) return '—';
  const n = Number(v);
  const a = Math.abs(n);
  if (a < 1_000)    return _decFmt(n);
  if (a < 100_000)  return `${Math.round(n / 1_000)}K`;
  return `${(n / 100_000).toFixed(2)}L`;
}

export function qtyFmt(v) {
  if (v == null || !isFinite(v)) return '—';
  return _IN0.format(Math.round(Number(v)));
}
