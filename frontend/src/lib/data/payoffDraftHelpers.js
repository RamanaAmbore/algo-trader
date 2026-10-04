// Pure helpers for payoffDrafts.svelte.js — deliberately split out of
// that file (which has module-level Svelte 5 $state) so these functions
// can be imported and unit-tested directly in Vitest without the Svelte
// compiler (same convention as nav.js / expiryPnl.js / derivativesMath.js,
// which portfolioStore.svelte.js imports its own pure logic from).
//
// No runes anywhere in this file.

import { decomposeSymbol } from './decomposeSymbol.js';
import { rootOf } from './rootOf.js';

/**
 * Convert a backend AlgoOrderInfo draft row (GET /drafts, POST /drafts's
 * own echo-shape built locally, or PATCH /drafts/{id}'s response) into
 * payoffDrafts.svelte.js's local Map-entry shape.
 *
 * @param {{id:number, symbol:string, exchange?:string, transaction_type?:string,
 *   quantity?:number, initial_price?:number|null, account?:string|null}} row
 */
export function draftRowToEntry(row) {
  const sym  = String(row.symbol || '').toUpperCase();
  const exch = String(row.exchange || '');
  const side = String(row.transaction_type || 'BUY').toUpperCase() === 'SELL' ? 'SELL' : 'BUY';
  const qtyAbs = Math.abs(Number(row.quantity || 0));
  const decomposed = decomposeSymbol(sym);
  return {
    id:               row.id,
    symbol:           sym,
    exchange:         exch,
    qty:              side === 'SELL' ? -qtyAbs : qtyAbs,
    avg_cost:         row.initial_price != null ? Number(row.initial_price) : null,
    ltp:              '',
    underlying:       rootOf(sym, exch),
    account:          String(row.account || ''),
    transaction_type: /** @type {'BUY'|'SELL'} */ (side),
    option_type:      decomposed.optType ?? null,
    strike:           decomposed.strike ?? null,
    expiry:           null,
    is_draft:         /** @type {true} */ (true),
  };
}

/**
 * Build the POST /drafts (or PATCH /drafts/{id}) request body from an
 * add()/update() entry. `qty` is signed (BUY positive, SELL negative);
 * the backend wants an unsigned `quantity` + explicit `transaction_type`.
 *
 * @param {{symbol: string, exchange?: string, qty: number,
 *   avg_cost?: number|null, account?: string|null}} entry
 */
export function draftEntryToBody(entry) {
  const qty = Number(entry.qty || 0);
  return {
    symbol:           String(entry.symbol || '').toUpperCase(),
    exchange:         String(entry.exchange || 'NFO'),
    transaction_type: /** @type {'BUY'|'SELL'} */ (qty < 0 ? 'SELL' : 'BUY'),
    // Contracts-equivalent, NOT lots — matches AlgoOrder.quantity's unit
    // everywhere downstream (see backend DraftOrderRequest docstring).
    quantity:         Math.abs(qty) || 1,
    price:            entry.avg_cost != null ? Number(entry.avg_cost) : null,
    account:          entry.account || null,
  };
}
