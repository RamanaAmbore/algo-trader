/**
 * watchlistContextActions.test.js — Vitest unit tests for
 * isSymbolInWatchlist / addSymbolToDefaultWatchlist /
 * removeSymbolFromWatchlist (2026-10-22 context-menu parity work).
 *
 * These are the generic Add/Remove-watchlist implementations every
 * SymbolContextMenu host EXCEPT MarketPulse uses (OrderBook, LogPanel,
 * PerformancePage, the derivatives Legs grid, /orders).
 *
 * Five quality dimensions:
 *  1. SSOT   — built directly on loadWatchlistSymbols()'s `membership`/
 *              `defaultListId` fields, the same cache every watchlist
 *              consumer in the app shares.
 *  2. Perf   — mocked I/O; all tests resolve in < 1 ms of CPU.
 *  3. Stale  — guards the global-list exclusion rule (a shared "Markets"
 *              seed list membership must never count as "in watchlist"
 *              or be removable from here) so a future refactor can't
 *              silently reintroduce the footgun the docblock warns about.
 *  4. Reuse  — exercises invalidateWatchlistSymbols() being called after
 *              every successful mutation, so the next menu-open sees
 *              fresh membership instead of a stale 30s-cached miss.
 *  5. UX     — the 409-dedup short-circuit (treat "already in watchlist"
 *              as success) mirrors MarketPulse's own addToWatchlistDeduped
 *              UX so the operator never sees a spurious error toast for
 *              an already-satisfied intent.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as api from '$lib/api';

vi.mock('$lib/api', () => ({
  fetchWatchlists:     vi.fn(),
  fetchWatchlist:      vi.fn(),
  addWatchlistItem:    vi.fn(),
  removeWatchlistItem: vi.fn(),
}));

const {
  isSymbolInWatchlist, addSymbolToDefaultWatchlist, removeSymbolFromWatchlist,
} = await import('$lib/data/watchlistContextActions.js');
const { invalidateWatchlistSymbols } = await import('$lib/data/watchlistSymbols.js');

const fetchWatchlists     = vi.mocked(api.fetchWatchlists);
const fetchWatchlist      = vi.mocked(api.fetchWatchlist);
const addWatchlistItem    = vi.mocked(api.addWatchlistItem);
const removeWatchlistItem = vi.mocked(api.removeWatchlistItem);

/** Seed a two-list watchlist universe: one personal default list with
 *  item NIFTY (id 1), one global "Markets" seed list also carrying
 *  NIFTY under a different item id (id 2) plus GOLD only on the global
 *  list. */
function _seedLists() {
  fetchWatchlists.mockResolvedValue([
    { id: 10, is_default: true,  is_global: false, is_pinned: false },
    { id: 20, is_default: false, is_global: true,  is_pinned: true },
  ]);
  fetchWatchlist.mockImplementation(async (id) => {
    if (id === 10) return { id: 10, items: [{ id: 1, tradingsymbol: 'NIFTY' }] };
    if (id === 20) return { id: 20, items: [
      { id: 2, tradingsymbol: 'NIFTY' },
      { id: 3, tradingsymbol: 'GOLD' },
    ] };
    return { id, items: [] };
  });
}

beforeEach(() => {
  invalidateWatchlistSymbols();
  vi.resetAllMocks();
});

describe('isSymbolInWatchlist', () => {
  it('is true for a symbol on the operator\'s own (non-global) list', async () => {
    _seedLists();
    expect(await isSymbolInWatchlist('NIFTY')).toBe(true);
  });

  it('is false for a symbol that only appears on a global/pinned seed list', async () => {
    _seedLists();
    expect(await isSymbolInWatchlist('GOLD')).toBe(false);
  });

  it('is false for a symbol on no list at all', async () => {
    _seedLists();
    expect(await isSymbolInWatchlist('RELIANCE')).toBe(false);
  });

  it('is false for an empty symbol without calling the API', async () => {
    expect(await isSymbolInWatchlist('')).toBe(false);
    expect(fetchWatchlists).not.toHaveBeenCalled();
  });

  it('is case-insensitive', async () => {
    _seedLists();
    expect(await isSymbolInWatchlist('nifty')).toBe(true);
  });
});

describe('addSymbolToDefaultWatchlist', () => {
  it('adds to the is_default list id', async () => {
    _seedLists();
    const r = await addSymbolToDefaultWatchlist('RELIANCE', 'NSE');
    expect(r.ok).toBe(true);
    expect(addWatchlistItem).toHaveBeenCalledWith(10, 'RELIANCE', 'NSE');
  });

  it('defaults the exchange to NSE when none is supplied', async () => {
    _seedLists();
    await addSymbolToDefaultWatchlist('RELIANCE', '');
    expect(addWatchlistItem).toHaveBeenCalledWith(10, 'RELIANCE', 'NSE');
  });

  it('invalidates the watchlist cache on success', async () => {
    _seedLists();
    addWatchlistItem.mockResolvedValue({});
    await addSymbolToDefaultWatchlist('RELIANCE', 'NSE');
    // A second load must re-hit the network — proves the cache was busted.
    await (await import('$lib/data/watchlistSymbols.js')).loadWatchlistSymbols();
    expect(fetchWatchlists).toHaveBeenCalledTimes(2);
  });

  it('treats a 409 "already in watchlist" failure as success', async () => {
    _seedLists();
    addWatchlistItem.mockRejectedValue(new Error('Symbol already in watchlist'));
    const r = await addSymbolToDefaultWatchlist('NIFTY', 'NSE');
    expect(r.ok).toBe(true);
  });

  it('reports failure with the broker/API error message on a real failure', async () => {
    _seedLists();
    addWatchlistItem.mockRejectedValue(new Error('network error'));
    const r = await addSymbolToDefaultWatchlist('RELIANCE', 'NSE');
    expect(r.ok).toBe(false);
    expect(r.reason).toBe('network error');
  });

  it('fails with "No watchlist available" when the operator has no personal list', async () => {
    fetchWatchlists.mockResolvedValue([
      { id: 20, is_default: false, is_global: true, is_pinned: true },
    ]);
    fetchWatchlist.mockResolvedValue({ id: 20, items: [] });
    const r = await addSymbolToDefaultWatchlist('RELIANCE', 'NSE');
    expect(r.ok).toBe(false);
    expect(r.reason).toBe('No watchlist available');
    expect(addWatchlistItem).not.toHaveBeenCalled();
  });

  it('fails fast on an empty symbol without calling the API', async () => {
    const r = await addSymbolToDefaultWatchlist('', 'NSE');
    expect(r.ok).toBe(false);
    expect(fetchWatchlists).not.toHaveBeenCalled();
  });
});

describe('removeSymbolFromWatchlist', () => {
  it('removes only the non-global membership, leaving the global one untouched', async () => {
    _seedLists();
    removeWatchlistItem.mockResolvedValue({});
    const r = await removeSymbolFromWatchlist('NIFTY');
    expect(r.ok).toBe(true);
    expect(removeWatchlistItem).toHaveBeenCalledTimes(1);
    expect(removeWatchlistItem).toHaveBeenCalledWith(10, 1);
    expect(removeWatchlistItem).not.toHaveBeenCalledWith(20, 2);
  });

  it('fails with "Not in watchlist" for a symbol only on a global seed list', async () => {
    _seedLists();
    const r = await removeSymbolFromWatchlist('GOLD');
    expect(r.ok).toBe(false);
    expect(r.reason).toBe('Not in watchlist');
    expect(removeWatchlistItem).not.toHaveBeenCalled();
  });

  it('invalidates the watchlist cache on success', async () => {
    _seedLists();
    removeWatchlistItem.mockResolvedValue({});
    await removeSymbolFromWatchlist('NIFTY');
    await (await import('$lib/data/watchlistSymbols.js')).loadWatchlistSymbols();
    expect(fetchWatchlists).toHaveBeenCalledTimes(2);
  });

  it('reports partial failure when the symbol is on multiple personal lists and one removal fails', async () => {
    fetchWatchlists.mockResolvedValue([
      { id: 10, is_default: true,  is_global: false, is_pinned: false },
      { id: 11, is_default: false, is_global: false, is_pinned: false },
    ]);
    fetchWatchlist.mockImplementation(async (id) => ({
      id, items: [{ id: id === 10 ? 1 : 2, tradingsymbol: 'NIFTY' }],
    }));
    removeWatchlistItem.mockImplementation(async (listId) => {
      if (listId === 11) throw new Error('fail');
      return {};
    });
    const r = await removeSymbolFromWatchlist('NIFTY');
    expect(r.ok).toBe(false);
    expect(r.reason).toBe('Some removals failed');
    expect(removeWatchlistItem).toHaveBeenCalledTimes(2);
  });

  it('fails fast on an empty symbol without calling the API', async () => {
    const r = await removeSymbolFromWatchlist('');
    expect(r.ok).toBe(false);
    expect(fetchWatchlists).not.toHaveBeenCalled();
  });
});
