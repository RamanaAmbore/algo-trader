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
 *   directional — sign-adjust an unsigned market move (Day change ₹ or %,
 *                 per-share or per-row) by the operator's net position
 *                 direction. For a short, a +1% market move is a -1%
 *                 loss; long / no-position rows pass through. Use this
 *                 wherever a per-share market metric is displayed next
 *                 to a position so the sign matches the operator's P&L
 *                 perspective. Backend aggregate fields (day_change_val,
 *                 day_change_percentage from /api/positions) already
 *                 carry the sign correctly — only the per-share market
 *                 values from /api/quote need this helper at the UI
 *                 layer.
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

// Prices ALWAYS render with 2 decimals so tick precision is visible.
// CRUDEOIL options tick at ₹0.10, NSE F&O at ₹0.05, MCX bullion at
// ₹0.05 — collapsing to integers (the previous shared _decFmt path)
// made operator see "₹552" for a real ₹552.30 order and conclude
// the price wasn't tick-aligned. Operator: "the price is not as per
// ticker size" — display bug, not a placement bug. Aggregates (P&L,
// money totals) keep the >=100 → integer collapse via aggFmt for
// scan-ability.
function _priceFmt(v) {
  if (v == null || !isFinite(v)) return '—';
  return _IN2.format(Number(v));
}

export const priceFmt = _priceFmt;
export const pctFmt   = _decFmt;
export const aggFmt   = _decFmt;

export function aggCompact(v) {
  if (v == null || !isFinite(v)) return '—';
  const n = Number(v);
  const a = Math.abs(n);
  if (a < 1_000)      return _decFmt(n);
  if (a < 100_000)    return `${Math.round(n / 1_000)}K`;
  if (a < 10_000_000) return `${(n / 100_000).toFixed(2)}L`;
  return `${(n / 10_000_000).toFixed(2)}C`;
}

export function qtyFmt(v) {
  if (v == null || !isFinite(v)) return '—';
  return _IN0.format(Math.round(Number(v)));
}

/**
 * Negate `value` when `netQty` is negative (net-short position) so an
 * unsigned market move displays from the operator's P&L perspective.
 *
 *   Long  / no-position (netQty ≥ 0): +1% market move → +1% display
 *   Short                (netQty <  0): +1% market move → -1% display
 *
 * Pass null through unchanged.
 */
export function directional(value, netQty) {
  if (value == null || !isFinite(value)) return value;
  return Number(netQty) < 0 ? -Number(value) : Number(value);
}
