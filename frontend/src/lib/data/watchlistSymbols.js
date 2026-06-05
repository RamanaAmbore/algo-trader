// Shared loader for "every tradingsymbol the operator has watchlisted".
// Used by SymbolSearchInput (Pinned section of the dropdown) and by
// MarketPulse (the unified grid's watchlist + pinned majors). Centralising
// the fetch + dedup pipeline here means the dropdown sees the SAME
// symbols Pulse shows — operator: "Symbol dropdown is not showing all
// the pinned symbols from pulse. every symbol dropdown should be
// consistent and use the same code".

import { fetchWatchlists, fetchWatchlist } from '$lib/api';

let _cache = /** @type {{ syms: string[]; lists: any[]; loadedAt: number } | null} */ (null);
const _TTL_MS = 30_000;

/** Force-refresh on next call. */
export function invalidateWatchlistSymbols() {
  _cache = null;
}

/**
 * Returns every tradingsymbol across EVERY watchlist the operator can see.
 * Lists with `is_pinned=true` (Default + Markets seeds) come first so the
 * dropdown's Pinned section reads in the same order as Pulse. Operator-
 * created lists follow. Symbol dedup is order-preserving.
 *
 * Cached in-memory for 30 s — auto-poll callsites still get fresh data
 * inside the same tick they need it.
 */
export async function loadWatchlistSymbols() {
  const now = Date.now();
  if (_cache && (now - _cache.loadedAt) < _TTL_MS) return _cache;

  try {
    const raw = await fetchWatchlists();
    const lists = Array.isArray(raw) ? raw : (raw?.watchlists ?? []);
    if (!lists.length) {
      _cache = { syms: [], lists: [], loadedAt: now };
      return _cache;
    }
    // Pinned + global first so the order matches Pulse's `pinned` major.
    const pinnedFirst = [
      ...lists.filter(l => l?.is_pinned || l?.is_global),
      ...lists.filter(l => !(l?.is_pinned || l?.is_global)),
    ];
    const details = await Promise.all(
      pinnedFirst.map(l => fetchWatchlist(l.id).catch(() => null))
    );
    /** @type {string[]} */ const syms = [];
    const seen = new Set();
    for (const d of details) {
      for (const it of (d?.items ?? [])) {
        const sym = String(it?.tradingsymbol ?? '').trim().toUpperCase();
        if (sym && !seen.has(sym)) {
          seen.add(sym);
          syms.push(sym);
        }
      }
    }
    _cache = { syms, lists, loadedAt: now };
    return _cache;
  } catch (_) {
    _cache = { syms: [], lists: [], loadedAt: now };
    return _cache;
  }
}
