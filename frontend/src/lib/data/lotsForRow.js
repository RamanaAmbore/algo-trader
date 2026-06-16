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
  const inst = getInstrument(sym);
  const itype = inst?.t;
  // Per-row quantities — MarketPulse splits pos vs hold; PerformancePage
  // sends the raw API rows with one `quantity` field. Fall through.
  const qPosRaw = row.qty_pos != null ? row.qty_pos
                : (itype === 'CE' || itype === 'PE' || itype === 'FUT')
                    ? row.quantity
                    : 0;
  const qHoldRaw = row.qty_hold != null ? row.qty_hold
                 : (itype === 'EQ' || itype === '' || itype == null)
                     ? row.quantity
                     : 0;
  const qPos = Math.abs(Number(qPosRaw) || 0);
  const qHold = Math.abs(Number(qHoldRaw) || 0);
  let total = 0;
  if (itype === 'CE' || itype === 'PE' || itype === 'FUT') {
    const lot = Number(inst?.ls) || 0;
    if (lot > 0 && qPos > 0) total += qPos / lot;
  } else {
    const lot = getOptionUnderlyingLot(sym);
    if (lot > 0 && qHold > 0) total += qHold / lot;
  }
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
