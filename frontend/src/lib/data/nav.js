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
 * Canonical base Day P&L for a single position row.
 *
 * Authoritative path (overnight positions already in daily_book):
 *   When `prev_settlement_pnl` is present and finite, Day P&L =
 *   `pnl − prev_settlement_pnl`. This is the delta since yesterday's
 *   settlement snapshot and is independent of `day_change_val` or
 *   `close_price` instability.
 *
 * Fallback path (new position opened today, not yet in daily_book):
 *   `prev_settlement_pnl` is null/absent. Compute cost-basis delta:
 *   `pnl − overnight_quantity × (close_price − average_price)`.
 *   This isolates the intraday component from the overnight unrealised carry.
 *
 * Mirrors the same guard used in:
 *   - derivatives/+page.svelte `_dayPnlForLeg` (non-expired branch)
 *   - derivatives/+page.svelte `_byUnderlyingTotals` loop
 *
 * Every frontend surface that renders a per-position Day P&L MUST call this
 * function (or a wrapper that calls it) instead of reading `p.day_change_val`
 * directly.
 *
 * @param {{ prev_settlement_pnl?: number|null, pnl?: number|null, overnight_quantity?: number|null, day_change_val?: number|null, close_price?: number|null, prev_close?: number|null, average_price?: number|null, avg_cost?: number|null }} p
 * @returns {number}
 */
export function baseDayPnlForPosition(p) {
  const pnl     = Number(p?.pnl ?? 0);
  const prevPnl = p?.prev_settlement_pnl;
  if (prevPnl != null && isFinite(prevPnl)) {
    // Authoritative: current P&L − yesterday's settlement P&L (from daily_book)
    return pnl - prevPnl;
  }
  // Fallback for positions opened today (not yet in daily_book)
  const oq  = Number(p?.overnight_quantity ?? 0);
  // Overnight hold with no prevPnl: use frozen day_change_val directly.
  // Avoids the close_price=0 trap post-MCX session when Kite returns stale zero.
  const dcv   = Number(p?.day_change_val ?? 0);
  const close = Number(p?.close_price ?? p?.prev_close ?? 0);
  const avg   = Number(p?.average_price ?? p?.avg_cost ?? 0);
  if (oq > 0 && dcv !== 0) return dcv;
  if (oq > 0 && dcv === 0) {
    // Case 4: only guard against zero/missing close (broker hasn't populated
    // prev_close yet). Removed close===ltp guard — it incorrectly zeroed realized
    // P&L when broker's close_price hadn't refreshed. Formula is correct regardless.
    if (close <= 0) return 0;
    return pnl - oq * (close - avg);
  }
  return pnl - oq * (close - avg);
}

/** Aggregate Day P&L for a positions array, applying the new-position override. SSOT for all TOTAL row day_pnl calculations. */
export function aggregateDayPnlForPositions(rows) {
  return rows.reduce((sum, r) => sum + baseDayPnlForPosition(r), 0);
}

/**
 * Live-LTP-aware Day P&L for a single position.
 *
 * Extends `baseDayPnlForPosition` with a live-tick rescue path for the
 * MCX stale-ticker fingerprint: when the broker REST endpoint ships
 * `last_price === close_price` (KiteTicker lag observed for CRUDEOIL
 * options around session open), `day_change_val` collapses to 0. Pulse
 * rescues this by recomputing via `(liveLtp − closePx) × qty` when a
 * live SSE tick is available; Derivatives was not applying the same
 * rescue — causing 0 instead of the correct Day P&L. This helper is
 * the canonical implementation shared by both surfaces so they cannot
 * drift.
 *
 * Recompute path (applied only when ALL of these hold):
 *   - `marketOpen` is true
 *   - `liveLtp` is a positive finite number (from SSE / symbolStore)
 *   - `closePx` > 0 and `qty` ≠ 0
 *
 * When the recompute applies:
 *   realisedToday = brokerDcv − (pollLtp − closePx) × qty
 *   result        = realisedToday + (liveLtp − closePx) × qty
 *
 * Fallback: contracts opened today (closePx = 0, avg > 0):
 *   result = (liveLtp − avg) × qty
 *
 * Otherwise: `baseDayPnlForPosition(dcvRow)`.
 *
 * IMPORTANT — the two callers use different field names:
 *   - Pulse (raw broker row): close_price, last_price, quantity, average_price
 *   - Derivatives (normalised candidate): prev_close, ltp, qty, avg_cost
 * Both callers normalise to explicit params before calling this helper.
 *
 * @param {{
 *   closePx:  number,  // prev session close  (r.close_price / c.prev_close)
 *   pollLtp:  number,  // LTP at last broker poll (r.last_price / c.ltp)
 *   qty:      number,  // signed net qty
 *   avg:      number,  // average cost per unit
 *   dcvRow:   object,  // raw row for baseDayPnlForPosition (needs day_change_val / overnight_quantity / pnl)
 * }} fields
 * @param {number|null|undefined} liveLtp  - live SSE tick LTP for this leg's own symbol
 * @param {{ marketOpen: boolean }} opts
 * @returns {number}
 */
export function livePositionDayPnl({ closePx, pollLtp, qty, avg, dcvRow }, liveLtp, { marketOpen }) {
  const brokerDcv = baseDayPnlForPosition(dcvRow);
  const live = (marketOpen && Number(liveLtp) > 0) ? Number(liveLtp) : null;
  if (live != null && closePx > 0 && qty !== 0) {
    // Realised-today component: broker's dcv minus the overnight mark-to-close
    // residual, so adding the live residual gives the full intraday P&L.
    const realisedToday = (pollLtp > 0 && closePx > 0)
      ? brokerDcv - (pollLtp - closePx) * qty
      : 0;
    return realisedToday + (live - closePx) * qty;
  }
  if (marketOpen && live != null && closePx === 0 && avg > 0 && qty !== 0) {
    // Position opened today — no prior close, track from avg_cost.
    return (live - avg) * qty;
  }
  return brokerDcv;
}

/**
 * Compute today's day P&L and lifetime P&L for F&O/derivative positions only.
 * Excludes equity (NSE/BSE) positions to avoid double-counting with the H pill.
 *
 * Applies `baseDayPnlForPosition` so the new-position override (`oq=0 → pnl`)
 * is consistent with the derivatives Snapshot / Legs / Exp Close / Payoff
 * overlay surfaces that all call `_dayPnlForLeg`.
 *
 * @param {Array<{exchange?: string, pnl?: number, day_change_val?: number, overnight_quantity?: number}>} positions
 * @returns {{ pnlTotal: number, dayTotal: number }}
 */
function positionsPnlFiltered(positions) {
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
