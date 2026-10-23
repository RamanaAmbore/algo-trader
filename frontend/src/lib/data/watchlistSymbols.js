// Shared loader for "every tradingsymbol the operator has watchlisted".
// Used by SymbolSearchInput (Pinned section of the dropdown) and by
// MarketPulse (the unified grid's watchlist + pinned majors). Centralising
// the fetch + dedup pipeline here means the dropdown sees the SAME
// symbols Pulse shows — operator: "Symbol dropdown is not showing all
// the pinned symbols from pulse. every symbol dropdown should be
// consistent and use the same code".
//
// `membership` + `defaultListId` (2026-10-22 context-menu parity work)
// let non-Pulse surfaces (OrderBook, LogPanel, PerformancePage, the
// derivatives Legs grid) implement the unified SymbolContextMenu's
// Add/Remove-watchlist item without re-building MarketPulse's own
// lists/activeIds/focusedListId state machine — see
// `watchlistContextActions.js` for the actions built on top of this.

import { fetchWatchlists, fetchWatchlist } from '$lib/api';

let _cache = /** @type {{
 *   syms: string[]; pinnedSyms: string[]; regularSyms: string[]; lists: any[]; loadedAt: number;
 *   membership: Map<string, Array<{ listId: number, itemId: number, isGlobal: boolean }>>;
 *   defaultListId: number | null;
 * } | null} */ (null);
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
      _cache = {
        syms: [], pinnedSyms: [], regularSyms: [], lists: [], loadedAt: now,
        membership: new Map(), defaultListId: null,
      };
      return _cache;
    }
    // Pinned + global first so the order matches Pulse's `pinned` major.
    const pinnedLists  = lists.filter(l => l?.is_pinned || l?.is_global);
    const regularLists = lists.filter(l => !(l?.is_pinned || l?.is_global));
    const pinnedFirst  = [...pinnedLists, ...regularLists];
    const details = await Promise.all(
      pinnedFirst.map(l => fetchWatchlist(l.id).catch(() => null))
    );
    const pinnedDetails  = details.slice(0, pinnedLists.length);
    const regularDetails = details.slice(pinnedLists.length);

    /** @type {string[]} */ const pinnedSyms = [];
    /** @type {string[]} */ const regularSyms = [];
    /** @type {string[]} */ const syms = [];
    const seen = new Set();
    /** @type {Map<string, Array<{ listId: number, itemId: number, isGlobal: boolean }>>} */
    const membership = new Map();

    /** @param {any} list @param {any} detail */
    function _recordMembership(list, detail) {
      for (const it of (detail?.items ?? [])) {
        const sym = String(it?.tradingsymbol ?? '').trim().toUpperCase();
        if (!sym || it?.id == null) continue;
        const entry = { listId: list.id, itemId: it.id, isGlobal: !!list?.is_global };
        const arr = membership.get(sym);
        if (arr) arr.push(entry); else membership.set(sym, [entry]);
      }
    }

    for (let i = 0; i < pinnedLists.length; i++) {
      const list = pinnedLists[i];
      const d = pinnedDetails[i];
      _recordMembership(list, d);
      for (const it of (d?.items ?? [])) {
        const sym = String(it?.tradingsymbol ?? '').trim().toUpperCase();
        if (sym && !seen.has(sym)) { seen.add(sym); syms.push(sym); pinnedSyms.push(sym); }
      }
    }
    for (let i = 0; i < regularLists.length; i++) {
      const list = regularLists[i];
      const d = regularDetails[i];
      _recordMembership(list, d);
      for (const it of (d?.items ?? [])) {
        const sym = String(it?.tradingsymbol ?? '').trim().toUpperCase();
        if (sym && !seen.has(sym)) { seen.add(sym); syms.push(sym); regularSyms.push(sym); }
      }
    }

    // Default add-target for surfaces with no "focused watchlist" concept
    // of their own (everything except MarketPulse): the operator's own
    // is_default list, else the first non-global list (a global/shared
    // seed list is never a sane default add-target for one operator's
    // own symbol), else null (operator has no personal list — Add to
    // watchlist stays hidden at the call site).
    const defaultListId =
      lists.find(l => l?.is_default)?.id
      ?? regularLists[0]?.id
      ?? pinnedLists.find(l => !l?.is_global)?.id
      ?? null;

    _cache = { syms, pinnedSyms, regularSyms, lists, loadedAt: now, membership, defaultListId };
    return _cache;
  } catch (_) {
    _cache = {
      syms: [], pinnedSyms: [], regularSyms: [], lists: [], loadedAt: now,
      membership: new Map(), defaultListId: null,
    };
    return _cache;
  }
}
