// Single source of truth for the per-account NAV breakdown formula
// (v4). Both PerformancePage's navByAcct grid and the NavBreakdown card
// import + consume this so they cannot drift.
//
// Backend equivalent: `backend/api/algo/nav.py:compute_firm_nav`.
//
// NAV formula (per account):
//   cash    = cash_sod + option_premium       (from funds row)
//   pos_m2m = Σ position.unrealised           (broker MTM)
//   hold    = Σ holdings.cur_val              (broker qty × LTP)
//   nav     = cash + pos_m2m + hold
//
// `option_premium` replaces the v3 `used_margin` term to eliminate the
// double-count of futures SPAN already inside position.unrealised.

/**
 * Compute the NAV breakdown row for a single account.
 * @param {string} acct
 * @param {Array<{account?: string, cash?: number, option_premium?: number}>} funds
 * @param {Array<{account?: string, unrealised?: number}>} positions
 * @param {Array<{account?: string, cur_val?: number}>} holdings
 * @returns {{
 *   account: string,
 *   cash: number,
 *   pos_m2m: number,
 *   holdings_mtm: number,
 *   nav: number,
 * }}
 */
function navRowForAccount(acct, funds, positions, holdings) {
  const fundsRow    = (funds ?? []).find(r => r.account === acct);
  const cash_sod    = Number(fundsRow?.cash) || 0;
  const opt_premium = Number(fundsRow?.option_premium) || 0;
  const cash_total  = cash_sod + opt_premium;
  const pos_m2m = (positions ?? [])
    .filter(r => r.account === acct)
    .reduce((s, r) => s + (Number(r.unrealised) || 0), 0);
  const holdings_mtm = (holdings ?? [])
    .filter(r => r.account === acct)
    .reduce((s, r) => s + (Number(r.cur_val) || 0), 0);
  return {
    account: acct,
    cash: cash_total,
    pos_m2m,
    holdings_mtm,
    nav: cash_total + pos_m2m + holdings_mtm,
  };
}

/**
 * Compute the NAV breakdown for a list of accounts.
 * @param {string[]} accounts
 * @param {Array<{account?: string, cash?: number, option_premium?: number}>} funds
 * @param {Array<{account?: string, unrealised?: number}>} positions
 * @param {Array<{account?: string, cur_val?: number}>} holdings
 */
export function navByAccount(accounts, funds, positions, holdings) {
  return (accounts ?? []).map(a => navRowForAccount(a, funds, positions, holdings));
}

// Exchanges that carry derivatives positions (F&O, commodity, currency).
// Equity CNC/MIS positions (exchange = "NSE" / "BSE") are excluded so the
// P pill doesn't double-count with the H pill which covers holdings day MTM.
// Note: MIS-only equity intraday positions (bought+squared, never in holdings)
// are also excluded under this filter — acceptable for an F&O-primary book.
/** Exchanges that carry F&O/derivative positions. Used by P-pill filter in PositionStrip. */
export const FO_EXCHANGES = new Set(['NFO', 'MCX', 'CDS', 'BFO']);

/**
 * Current total profit for a position row — `realised + unrealised` when
 * both are present and finite; falls back to the broker-combined `pnl`
 * field otherwise (Kite's native `pnl` is confirmed = realised+unrealised
 * per Zerodha's own forum statement, and this fallback also covers rows
 * from the persistent cache layer, closed-hours snapshots, and any
 * pre-deploy window where the split fields aren't yet populated).
 *
 * @param {{ realised?: number|null, unrealised?: number|null, pnl?: number|null }} p
 * @returns {number}
 */
export function currentTotalProfit(p) {
  const realised   = Number(p?.realised) || 0;
  const unrealised = Number(p?.unrealised) || 0;
  // Mirrors backend's resolve_realised_unrealised (pnl_math.py) EXACTLY:
  // only fall back to `pnl` when BOTH legs are exactly 0 (not split /
  // not populated). A legitimately-zero single leg (e.g. a fresh open
  // position with realised=0, unrealised>0) must NOT trigger the
  // fallback — must stay in lockstep with the backend rule or live and
  // closed-hours-served rows can silently disagree.
  if (realised || unrealised) {
    return realised + unrealised;
  }
  return Number(p?.pnl) || 0;
}

/**
 * Canonical base Day P&L for a single position row.
 *
 * Atomic formula (proven correct for any position state — new entry, full
 * exit, partial exit, re-entry, flip): `current_total_profit − base_pnl`,
 * where `base_pnl` is that position's total profit frozen at the most
 * recent trading day's close-reset snapshot (0 when none exists, e.g. a
 * position opened today). See `currentTotalProfit` for the realised+
 * unrealised (Kite-pnl-fallback) sourcing.
 *
 * Every frontend surface that renders a per-position Day P&L MUST call this
 * function (or a wrapper that calls it) instead of reading a raw broker
 * day-change field directly.
 *
 * @param {{ realised?: number|null, unrealised?: number|null, pnl?: number|null, prev_settlement_pnl?: number|null, tradingsymbol?: string|null, symbol?: string|null }} p
 * @returns {number}
 */
export function baseDayPnlForPosition(p) {
  const total    = currentTotalProfit(p);
  const basePnl  = p?.prev_settlement_pnl;
  const base     = (basePnl != null && isFinite(Number(basePnl))) ? Number(basePnl) : 0;
  return total - base;
}

/**
 * Aggregate Day P&L for a positions array — sums `baseDayPnlForPosition(r)`
 * (the atomic baseline-diff formula) across every row. SSOT for all TOTAL
 * row day_pnl calculations.
 */
export function aggregateDayPnlForPositions(rows) {
  return rows.reduce((sum, r) => sum + baseDayPnlForPosition(r), 0);
}

/**
 * Compute today's day P&L and lifetime P&L for F&O/derivative positions only.
 * Excludes equity (NSE/BSE) positions to avoid double-counting with the H pill.
 *
 * Applies `baseDayPnlForPosition` (the atomic baseline-diff formula:
 * `current_total_profit − base_pnl`) per row, so this total is consistent
 * with the derivatives Snapshot / Legs / Exp Close / Payoff overlay surfaces,
 * which compute their own per-leg Day P&L via the same helper.
 *
 * @param {Array<{exchange?: string, pnl?: number, realised?: number|null, unrealised?: number|null, prev_settlement_pnl?: number|null}>} positions
 * @returns {{ pnlTotal: number, dayTotal: number }}
 */
export function positionsPnlFiltered(positions) {
  let pnlTotal = 0;
  let dayTotal  = 0;
  for (const p of (positions ?? [])) {
    const exch = String(p?.exchange || '').toUpperCase();
    if (!FO_EXCHANGES.has(exch)) continue;
    pnlTotal += Number(p?.pnl || 0);
    dayTotal  += baseDayPnlForPosition(p);
  }
  return { pnlTotal, dayTotal };
}

/**
 * Compute day-change percentage from day P&L and previous market value.
 * Returns null (not 0) when the denominator is non-positive or inputs are
 * non-finite — callers use `?? 0` when a numeric fallback is required.
 *
 * @param {number|null|undefined} dayPnl
 * @param {number|null|undefined} prevMv  - previous market value (close × qty)
 * @returns {number|null}
 */
export function dayChangePct(dayPnl, prevMv) {
  const dpnl = Number(dayPnl), mv = Number(prevMv);
  if (!Number.isFinite(dpnl) || mv <= 0) return null;
  return (dpnl / mv) * 100;
}

/**
 * Sum a list of nav rows into a TOTAL row. Returns null on empty.
 * @param {Array<ReturnType<typeof navRowForAccount>>} rows
 */
export function navTotalRow(rows) {
  if (!rows || rows.length === 0) return null;
  return rows.reduce((acc, r) => ({
    account:      'TOTAL',
    cash:         acc.cash         + r.cash,
    pos_m2m:      acc.pos_m2m      + r.pos_m2m,
    holdings_mtm: acc.holdings_mtm + r.holdings_mtm,
    nav:          acc.nav          + r.nav,
  }), { account: 'TOTAL', cash: 0, pos_m2m: 0, holdings_mtm: 0, nav: 0 });
}
