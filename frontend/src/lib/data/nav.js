// NAV SSOT (2026-09 consolidation): the per-account NAV breakdown formula
// (v4) is computed EXCLUSIVELY by the backend now —
// `backend/api/algo/nav.py:compute_firm_nav()["by_account"]`, served via
// `GET /api/nav/by-account`. PerformancePage's NAV grid, NavCard's FIRM
// NAV panel (via /api/auth/firm-nav and /api/auth/me/nav), and the
// dashboard NAV chip (NavTab, via /api/auth/firm-nav) all ultimately read
// numbers derived from that one backend call — there is no parallel
// client-side NAV formula left in this file.
//
// This module used to also carry `navRowForAccount`/`navByAccount` — a
// client-side re-implementation of the v4 formula (removed 2026-09).
// That formula had already drifted from the backend's: it summed
// `unrealised` unconditionally (backend gates on qty != 0), it never
// added the `realised` leg at all (so a same-day full position exit
// silently vanished from NAV in the grid while showing correctly in
// NavCard), and it had no ticker-rescue fallback for stale/zero-LTP
// holdings rows. Eliminating the duplicate — not patching it to match —
// is the fix: any future formula revision now only touches nav.py.
//
// `navTotalRow` (below) is NOT a NAV formula — it's a pure row-summation
// helper for re-aggregating an already-server-computed per-account rows
// array into a TOTAL row after a client-side account filter is applied
// (e.g. PerformancePage's account picker). Safe to keep: summing
// already-correct numbers can't introduce drift.

import { isFOSymbol } from '$lib/data/derivativesMath.js';

// positionsPnlFiltered (below) excludes equity CNC/MIS positions so the
// P pill doesn't double-count with the H pill which covers holdings day MTM.
// Note: MIS-only equity intraday positions (bought+squared, never in holdings)
// are also excluded under this filter — acceptable for an F&O-primary book.
//
// 2026-09 R4 post-ship audit fix: classification used to be the exchange-set
// gate `FO_EXCHANGES = {NFO,MCX,CDS,BFO}` (removed — see git history if the
// exchange-based set is ever needed again), which silently excluded a
// Groww-sourced F&O row reporting `exchange:'NSE'` for an actual NFO
// contract. Now uses the shared `isFOSymbol` predicate (import above),
// same as every other F&O-classification site (Commit 7).

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
 * Classification uses the shared `isFOSymbol` predicate (2026-09 R4 fix),
 * not the `exchange` field — a Groww-sourced F&O row whose adapter passes
 * `exchange` through unchanged (e.g. reporting 'NSE' for an actual NFO
 * contract) would have been silently excluded by the old `FO_EXCHANGES`
 * gate even though every other F&O-classification site in the app (Commit 7)
 * already agreed it's F&O.
 *
 * @param {Array<{exchange?: string, tradingsymbol?: string|null, symbol?: string|null, pnl?: number, realised?: number|null, unrealised?: number|null, prev_settlement_pnl?: number|null}>} positions
 * @returns {{ pnlTotal: number, dayTotal: number }}
 */
export function positionsPnlFiltered(positions) {
  let pnlTotal = 0;
  let dayTotal  = 0;
  for (const p of (positions ?? [])) {
    const sym = p?.tradingsymbol || p?.symbol;
    if (!isFOSymbol(sym)) continue;
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
 * Sum a list of NAV-by-account rows (as returned by `GET /api/nav/by-account`,
 * or a client-filtered subset of them) into a TOTAL row. Pure summation —
 * not a formula — so it can't drift from the server-computed inputs.
 * Returns null on empty.
 * @param {Array<{account: string, cash: number, pos_m2m: number, holdings_mtm: number, nav: number}>} rows
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
