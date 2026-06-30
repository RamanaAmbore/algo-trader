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


// ── Percentage helpers ───────────────────────────────────────────────────────
//
// Two variants for the two input semantics found across the app:
//
//   fmtPctScaled    — input is ALREADY a percentage (5.0 → "5.0%").
//                     Used by surfaces that consume API fields like
//                     `pnl_percentage`, `day_change_percentage` —
//                     backend pre-scales these.
//   fmtPctFraction  — input is a FRACTION (0.05 → "5.0%"). Used by
//                     options analytics (`ev_pct`, intrinsic ratios,
//                     greeks like delta) where the math returns a
//                     fraction.
//
// Both honour the same null-rendering rule as the other helpers
// (`—` for null/undefined/NaN/Infinity) and respect `decimals` (the
// `toFixed` precision) so callers can opt into more / fewer digits
// without re-implementing the formatter.
//
// `signed=true` prepends `+` to positive values — used by surfaces
// where color isn't enough on its own (e.g. small inline chips in
// templates list).
//
// Why not a single helper with a `scaled` flag? Two reasons:
//   1. Call-site readability — `fmtPctFraction(0.05)` makes it
//      obvious that the input is a fraction.
//   2. Lint-able SSOT — a grep for `fmtPct` finds 3 inconsistent
//      legacy variants; splitting forces the contributor to pick
//      the right one.

/**
 * Format an already-percent-scaled value. Examples:
 *   fmtPctScaled(5.0)         → "5.00%"      (|v| < 100 → 2 decimals)
 *   fmtPctScaled(5.0, 1)      → "5.0%"
 *   fmtPctScaled(5.0, 1, true) → "+5.0%"
 *
 * Pass `decimals` to override the default decimal-rule formatting
 * (the same |v| < 100 → 2dp rule used by pctFmt). When omitted, the
 * default rule applies.
 */
export function fmtPctScaled(v, decimals = null, signed = false) {
  if (v == null || !isFinite(v)) return '—';
  const n = Number(v);
  // `+` for non-negative (matches the legacy templates-list formatter
  // where exactly 0% renders as `+0.0%` to keep the column rigid).
  const sign = signed && n >= 0 ? '+' : '';
  if (decimals != null) return `${sign}${n.toFixed(decimals)}%`;
  return `${sign}${pctFmt(n)}%`;
}

/**
 * Format a fractional value as a percentage. Examples:
 *   fmtPctFraction(0.05)        → "5.00%"
 *   fmtPctFraction(0.05, 1)     → "5.0%"
 *   fmtPctFraction(0.05, 1, true) → "+5.0%"
 */
export function fmtPctFraction(v, decimals = null, signed = false) {
  if (v == null || !isFinite(v)) return '—';
  return fmtPctScaled(Number(v) * 100, decimals, signed);
}
