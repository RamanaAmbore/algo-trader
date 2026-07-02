/**
 * Shared expiry-day P&L helper — single source of truth used by the
 * derivatives-page Snapshot Exp P&L column, the payoff overlay legs
 * TOTAL row, AND the NavStrip P pill slot 3.
 *
 * Operator 2026-07-01: "use the same number to update p 3 values in
 * navstrip." Formerly each surface had its own inline math; small
 * divergences (strike-parse regex, sign convention on shorts) drifted
 * the numbers between the three views. Consolidating the compute keeps
 * them locked.
 *
 * Contract:
 *   - Options: `(intrinsic − avg_cost) × qty` where intrinsic is the
 *     option's payoff at spot (max(0, spot − strike) for CE, mirror for PE).
 *     qty preserves its signed sense from Kite (positive long / negative short)
 *     so writing a call yields negative-qty × negative-intrinsic-delta
 *     surfaces the credit correctly.
 *   - Futures + equity: `(spot − avg_cost) × qty` — no time value to strip,
 *     P&L tracks spot 1:1.
 *
 * Returns null when the input is unusable (spot missing / non-positive,
 * qty=0, cost=0, strike/opt_type unparseable). The caller decides how
 * to render "—" for null rows.
 *
 * @param {{ symbol: string, qty: number|string, avg_cost: number|string, kind: 'opt'|'fut'|'eq'|string }} c
 * @param {number|null|undefined} spot   underlying spot for intrinsic calculation
 * @param {Record<string, {strike?: number, opt_type?: string}>} [legAnalyticsBySymbol]
 *   optional map of symbol → backend leg analytics (strike + opt_type
 *   from strategy-analytics response) — preferred over the regex parse
 *   when available.
 * @returns {number|null}
 */
export function expiryPnl(c, spot, legAnalyticsBySymbol = {}) {
  if (spot == null || !isFinite(Number(spot)) || Number(spot) <= 0) return null;
  const qty = Number(c?.qty || 0);
  const cost = Number(c?.avg_cost || 0);
  if (!qty || !cost) return null;
  const S = Number(spot);
  if (c?.kind === 'opt') {
    const sym = String(c?.symbol || '');
    const lg = legAnalyticsBySymbol?.[sym];
    let K = lg?.strike ?? null;
    let opt = lg?.opt_type ?? null;
    if (K == null || !opt) {
      const m = /(\d+(?:\.\d+)?)(CE|PE)$/i.exec(sym);
      if (m) { K = Number(m[1]); opt = m[2].toUpperCase(); }
    }
    if (K == null || !opt) return null;
    const intrinsic = opt === 'CE' ? Math.max(0, S - K) : Math.max(0, K - S);
    return (intrinsic - cost) * qty;
  }
  // futures + equity: P&L tracks spot 1:1.
  return (S - cost) * qty;
}
