/**
 * Maps (side, symbol) → template applies_to scope string.
 * Shared by OrderTicket and SymbolPanel — SSOT.
 *
 * Returns:
 *   'sell_option' — SELL + CE/PE symbol (protective wing scope)
 *   'sell_any'    — SELL, non-option
 *   'buy_option'  — BUY  + CE/PE symbol
 *   'buy_any'     — BUY,  non-option
 *   'both'        — catch-all (no directional filter)
 */
export function appliesToFor(side, sym) {
  if (side === 'SELL' && /\d+(CE|PE)$/i.test(sym || '')) return 'sell_option';
  if (side === 'SELL') return 'sell_any';
  if (side === 'BUY'  && /\d+(CE|PE)$/i.test(sym || '')) return 'buy_option';
  if (side === 'BUY')  return 'buy_any';
  return 'both';
}
