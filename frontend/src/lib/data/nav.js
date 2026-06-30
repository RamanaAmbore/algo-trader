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
export function navRowForAccount(acct, funds, positions, holdings) {
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
