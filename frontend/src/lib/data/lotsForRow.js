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
    qPos:  Math.abs(Number(qPosRaw)  || 0),
    qHold: Math.abs(Number(qHoldRaw) || 0),
  };
}

/**
 * Compute the raw (un-rounded) lot total for the given instrument.
 * @param {string} sym
 * @param {string|undefined|null} itype
 * @param {number} qPos
 * @param {number} qHold
 * @param {any} inst
 * @returns {number}
 */
function _computeLots(sym, itype, qPos, qHold, inst) {
  const isDerivative = itype === 'CE' || itype === 'PE' || itype === 'FUT';
  if (isDerivative) {
    const lot = Number(inst?.ls) || 0;
    return (lot > 0 && qPos > 0) ? qPos / lot : 0;
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
  const total = _computeLots(sym, itype, qPos, qHold, inst);
  return Math.round(total * 10) / 10;
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
