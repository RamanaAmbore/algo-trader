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
 * Helpers:
 *   priceFmt    — per-share prices (LTP, avg, fill, slippage). 2 dp under
 *                 ₹500, 0 dp at/above (decimals add noise on big spots).
 *   pctFmt      — percentages / ratios (2 dp, caller appends '%').
 *   aggFmt      — ₹ aggregates with full digits (P&L, margins, cash).
 *                 Used when columns aren't tight on space.
 *   aggCompact  — Indian-scale compact ₹ aggregates for tight columns.
 *                 < 1,000 → plain; < 1,00,000 → "K"; ≥ 1,00,000 → "X.XXL".
 *                 Use this for /performance / /dashboard / strip / log.
 *   qtyFmt      — share/lot counts (alias of aggFmt — full digits).
 *
 * To change the K/L thresholds, decimal counts, or any prefix rule —
 * edit this file. Do NOT add `₹` / `+` / lakh-conversion logic at
 * call sites; the call sites should consume these helpers verbatim.
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

/** Compact Indian-scale format for ₹ aggregates in tight columns:
 *    |v| < 1,000     → plain en-IN ("999", "-432")
 *    |v| < 1,00,000  → rounded thousand + "K" ("50K", "100K")
 *    |v| ≥ 1,00,000  → lakhs + 2dp + "L" ("1.50L", "150.00L")
 *  Single source of truth — change here to reformat every grid + strip
 *  + log surface that uses it. */
export function aggCompact(v) {
  if (v == null || !isFinite(v)) return '—';
  const n = Number(v);
  const a = Math.abs(n);
  if (a < 1_000)    return _IN0.format(Math.round(n));
  if (a < 100_000)  return `${Math.round(n / 1_000)}K`;
  return `${(n / 100_000).toFixed(2)}L`;
}

/** Alias of aggFmt — for share/lot counts. */
export const qtyFmt = aggFmt;
