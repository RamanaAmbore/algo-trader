/**
 * watchlistContextActions.js — Add/Remove-watchlist for the unified
 * SymbolContextMenu on surfaces that have no "focused watchlist" concept
 * of their own (OrderBook, LogPanel, PerformancePage, the derivatives
 * Legs grid, /orders). MarketPulse keeps its own richer
 * ctxAddWatch/ctxRemoveWatch (targets whichever list is focused in its
 * own UI) — these are the generic fallback: Add always targets the
 * operator's default list (see `loadWatchlistSymbols()`'s
 * `defaultListId`); Remove removes every non-global membership (a
 * symbol can legitimately be in more than one list, and none of these
 * surfaces has a specific-list context to disambiguate, so "Remove from
 * watchlist" means "take this off every personal list I can see it on").
 *
 * Membership in a shared/global seed list (e.g. the "Markets" pinned
 * list) does NOT count as "in watchlist" here and is never removed by
 * this module — only the operator's own admin/designated session can
 * mutate a global list, and silently stripping a shared row out from
 * under every other user on a single operator's context-menu click
 * would be a real footgun.
 */

import { addWatchlistItem, removeWatchlistItem } from '$lib/api';
import { loadWatchlistSymbols, invalidateWatchlistSymbols } from './watchlistSymbols.js';

/**
 * @param {string} tradingsymbol
 * @returns {Promise<boolean>} true when the symbol is on at least one of
 *   the operator's own (non-global) watchlists.
 */
export async function isSymbolInWatchlist(tradingsymbol) {
  const sym = String(tradingsymbol || '').toUpperCase();
  if (!sym) return false;
  const { membership } = await loadWatchlistSymbols();
  const entries = membership.get(sym) || [];
  return entries.some((e) => !e.isGlobal);
}

/**
 * Add a symbol to the operator's default watchlist.
 *
 * @param {string} tradingsymbol
 * @param {string} exchange
 * @returns {Promise<{ ok: boolean, reason?: string }>}
 */
export async function addSymbolToDefaultWatchlist(tradingsymbol, exchange) {
  const sym = String(tradingsymbol || '').trim();
  if (!sym) return { ok: false, reason: 'No symbol' };
  const { defaultListId } = await loadWatchlistSymbols();
  if (defaultListId == null) return { ok: false, reason: 'No watchlist available' };
  try {
    await addWatchlistItem(defaultListId, sym, exchange || 'NSE');
    invalidateWatchlistSymbols();
    return { ok: true };
  } catch (e) {
    // 409 = already present — treat as success, the operator's intent is
    // already satisfied (mirrors MarketPulse's addToWatchlistDeduped).
    const msg = String(e?.message || '');
    if (/already/i.test(msg)) { invalidateWatchlistSymbols(); return { ok: true }; }
    return { ok: false, reason: msg || 'Add failed' };
  }
}

/**
 * Remove a symbol from every one of the operator's own (non-global)
 * watchlists it appears on.
 *
 * @param {string} tradingsymbol
 * @returns {Promise<{ ok: boolean, reason?: string }>}
 */
export async function removeSymbolFromWatchlist(tradingsymbol) {
  const sym = String(tradingsymbol || '').trim().toUpperCase();
  if (!sym) return { ok: false, reason: 'No symbol' };
  const { membership } = await loadWatchlistSymbols();
  const entries = (membership.get(sym) || []).filter((e) => !e.isGlobal);
  if (!entries.length) return { ok: false, reason: 'Not in watchlist' };
  const results = await Promise.all(
    entries.map((e) => removeWatchlistItem(e.listId, e.itemId).then(() => true).catch(() => false))
  );
  invalidateWatchlistSymbols();
  return results.every(Boolean)
    ? { ok: true }
    : { ok: false, reason: 'Some removals failed' };
}
