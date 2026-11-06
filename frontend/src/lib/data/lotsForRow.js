// Shared "qty expressed in F&O lot units" helper used across the
// Pulse, Dashboard, and Performance grids so every page reads the
// same number for the same row.
//
// Rule (Operator: "you can keep qty as lot size as a separate column.
// if it is not an underlying show it as 0… similarly do it for option
// positions for other positions show it as 0. keep it consistent
// across all algo pages for holdings and positions."):
//
//   Holding on an F&O underlying (EQ row with options listed)
//       → qty_hold / underlying_lot
//   Position on a derivative contract (CE / PE / FUT)
//       → qty_pos / contract_lot
//   Cash equity position, non-F&O holding, watchlist row, TOTAL row
//       → 0
//
// One decimal place when fractional, integer when whole. 0 renders
// as a bare "0" so operators scanning the column can skip rows
// instantly.

import { getInstrument, getOptionUnderlyingLot } from '$lib/data/instruments';
import { legDisplayQty } from '$lib/data/derivativesMath.js';

/**
 * Normalize raw row quantities into {qPos, qHold} unsigned values.
 * MarketPulse splits pos vs hold; PerformancePage sends one `quantity`
 * field — fall through based on instrument type.
 * @param {any} row
 * @param {string|undefined|null} itype
 * @returns {{qPos: number, qHold: number}}
 */
function _normalizeQtys(row, itype) {
  const isDerivative = itype === 'CE' || itype === 'PE' || itype === 'FUT';
  const isEquityLike  = itype === 'EQ' || itype === '' || itype == null;
  const qPosRaw  = row.qty_pos != null  ? row.qty_pos  : (isDerivative ? row.quantity : 0);
  const qHoldRaw = row.qty_hold != null ? row.qty_hold : (isEquityLike  ? row.quantity : 0);
  return {
    qPos:  Number(qPosRaw)  || 0,
    qHold: Math.abs(Number(qHoldRaw) || 0),
  };
}

/**
 * Compute the raw (un-rounded) lot total for the given instrument.
 *
 * Derivative path: prefer `row.lots` (new backend field, integer lot count)
 * to avoid a division by lot_size that would be wrong when the instruments
 * cache is cold (inst?.ls === undefined → lot = 0 → returns 0).
 * Fallback: `qPos / lot` is correct when `lots` is absent and the cache is
 * warm — the backend now returns `quantity` as contracts uniformly, so
 * contracts / lot_size = lot count.
 *
 * 2026-09 Commit 9 investigation: `backend/api/schemas.py`'s `PositionRow`
 * (the live `/api/positions` response struct) does NOT have a `lots`
 * field — the `lots`/`lot_size` DB columns added by an earlier commit
 * ("add lots/lot_size fields") live on the internal `daily_book` table for
 * settlement bookkeeping, not on the response schema this fast path reads.
 * So `row?.lots` is never populated for a real broker position/holding row
 * — BUT `derivatives/pageLoad.js:buildCandidatePositions` DOES set
 * `.lots` on provisional (post-fill, pre-broker-refresh) and draft-store
 * rows, sourced from the LOCAL order-fill event Maps
 * (`getProvisionalPositions()`/`getDraftPositions()`), a genuinely
 * different pipeline than `/api/positions`. NOT removed — this fast path
 * is reachable for that narrower row set, just not for a live broker row.
 *
 * @param {string} sym
 * @param {string|undefined|null} itype
 * @param {number} qPos
 * @param {number} qHold
 * @param {any} inst
 * @param {any} row  - the original row (for the `lots` shortcut)
 * @returns {number}
 */
function _computeLots(sym, itype, qPos, qHold, inst, row) {
  const isDerivative = itype === 'CE' || itype === 'PE' || itype === 'FUT';
  if (isDerivative) {
    // Fast path: backend-supplied lot count (available when API returns
    // the new `lots` field — avoids division and instruments-cache miss).
    if (row?.lots != null) return Number(row.lots) || 0;
    const lot = Number(inst?.ls) || 0;
    return (lot > 0 && qPos !== 0) ? qPos / lot : 0;
  }
  const lot = getOptionUnderlyingLot(sym);
  return (lot > 0 && qHold > 0) ? qHold / lot : 0;
}

/**
 * Compute the lot-count for a holdings/positions row.
 * Accepts either:
 *   - the unified MarketPulse row shape (tradingsymbol, qty_pos, qty_hold, _isTotal)
 *   - the raw API row shape (tradingsymbol, quantity, _isTotal)
 *
 * For the raw shape we use `quantity` as both pos and hold (the row
 * is either a position OR a holding, never both) and infer which by
 * the instrument type.
 *
 * @param {any} row
 * @returns {number|null} lot count, or null for TOTAL rows
 */
export function lotsForRow(row) {
  if (!row || row._isTotal) return null;
  const sym = String(row.tradingsymbol || '').toUpperCase();
  if (!sym) return 0;
  const inst  = getInstrument(sym);
  const itype = inst?.t;
  const { qPos, qHold } = _normalizeQtys(row, itype);
  const total = _computeLots(sym, itype, qPos, qHold, inst, row);
  return Math.round(total * 10) / 10;
}

/**
 * Lots value for the derivatives Candidates/Legs grid's own Lots column —
 * extracted VERBATIM (2026-11, no formula changes) from
 * CandidateLegRow.svelte's existing proxy-beta-scaling branch (a proxy ETF
 * leg like GOLDBEES scaled into the hedged root's own lot units) and its
 * plain `lotsForRow(...)` branch for every other row. Shared by the cell
 * render AND a column-sort accessor so they can never disagree.
 *
 * Mirrors the cell's own "waiting on spot" convention: the proxy branch
 * returns `null` (renders '—') when the scaled lot count isn't yet a
 * positive number; the plain branch returns `lotsForRow()`'s own value
 * as-is, including a genuine `0`.
 *
 * `proxyRow` is the caller's own `getProxyRow(c.symbol, c.proxy_for)`
 * lookup — this function deliberately does NOT import `hedgeProxies.js`
 * itself: that module's top-level `import { fetchHedgeProxies } from
 * '$lib/api'` chain reaches `stores.js` → `activityStore.svelte.js`
 * (a rune-using `.svelte.js` module), which throws under Vitest the
 * instant anything imports it (same constraint documented on
 * `legLiveLtp` above). `lotsForRow.js` is imported directly by its own
 * Vitest suite, so it must stay free of that chain.
 *
 * @param {any} c - candidate leg row
 * @param {any} strategy - current strategy object (for proxy spot math)
 * @param {any} [proxyRow] - caller's `getProxyRow(c.symbol, c.proxy_for)`, when `c.proxy_for` is set
 * @returns {number | null}
 */
export function legLotsValue(c, strategy, proxyRow) {
  const displayQty = legDisplayQty(c);
  if (c.proxy_for) {
    const targetSpot = Number(strategy?.spot) || 0;
    const proxyLtp = Number(c.ltp) || 0;
    const beta = proxyRow?.beta != null ? Number(proxyRow.beta) : 1.0;
    const targetLot = getOptionUnderlyingLot(c.proxy_for);
    const effQty = (targetSpot > 0 && proxyLtp > 0)
      ? (beta * Math.abs(displayQty) * proxyLtp) / targetSpot : 0;
    const targetLots = targetLot > 0 ? effQty / targetLot : 0;
    return targetLots > 0 ? targetLots : null;
  }
  return lotsForRow({ tradingsymbol: c.symbol, quantity: displayQty, lots: c.lots ?? null });
}

/**
 * Format a lots value for grid display.
 *   null      → ''   (TOTAL row, blank)
 *   0         → '0'
 *   integer N → 'N'
 *   fraction  → toFixed(1)
 * @param {number|null|undefined} value
 */
export function fmtLots(value) {
  if (value == null) return '';
  if (value === 0) return '0';
  return value % 1 === 0 ? String(value) : value.toFixed(1);
}
