/**
 * watchlistSymbols.test.js — Vitest unit tests for loadWatchlistSymbols().
 *
 * Five quality dimensions:
 *  1. SSOT  — exercises the same module path used by SymbolSearchInput,
 *             MarketPulse, and the derivatives picker.
 *  2. Perf  — mocked I/O; all tests resolve in < 1 ms of CPU.
 *  3. Stale — guards pinnedSyms / regularSyms split and dedup invariant
 *             so future refactors can't silently break the tier separation.
 *  4. Reuse — loadWatchlistSymbols() is the single fetch boundary for
 *             every watchlist consumer in the app.
 *  5. UX    — Tier 4 (pinned) and Tier 5 (regular) in the derivatives
 *             picker depend on the correctness of this split; verified here.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as api from '$lib/api';

// ─────────────────────────────────────────────────────────────────────────────
// Mock $lib/api before importing the module under test.
// ─────────────────────────────────────────────────────────────────────────────

vi.mock('$lib/api', () => ({
  fetchWatchlists: vi.fn(),
  fetchWatchlist:  vi.fn(),
}));

// Dynamic import so the mock is in place before module evaluation.
const { loadWatchlistSymbols, invalidateWatchlistSymbols } =
  await import('$lib/data/watchlistSymbols.js');

// vi.mocked() gives TypeScript the MockedFunction type so .mockResolvedValue etc. type-check.
const fetchWatchlists = vi.mocked(api.fetchWatchlists);
const fetchWatchlist  = vi.mocked(api.fetchWatchlist);

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

/** Build a minimal watchlist descriptor. */
function makeList(id, flags = {}) {
  return { id, is_pinned: false, is_global: false, is_default: false, ...flags };
}

/** Build a minimal watchlist detail response. */
function makeDetail(tradingsymbols = []) {
  return { items: tradingsymbols.map(t => ({ tradingsymbol: t })) };
}

// ─────────────────────────────────────────────────────────────────────────────
// Reset cache + mocks before every test.
// ─────────────────────────────────────────────────────────────────────────────

beforeEach(() => {
  invalidateWatchlistSymbols();
  vi.resetAllMocks();
});

// ─────────────────────────────────────────────────────────────────────────────
// Return shape
// ─────────────────────────────────────────────────────────────────────────────

describe('loadWatchlistSymbols — return shape', () => {
  it('always returns syms, pinnedSyms, regularSyms, lists, loadedAt', async () => {
    fetchWatchlists.mockResolvedValue([]);
    const result = await loadWatchlistSymbols();
    expect(result).toHaveProperty('syms');
    expect(result).toHaveProperty('pinnedSyms');
    expect(result).toHaveProperty('regularSyms');
    expect(result).toHaveProperty('lists');
    expect(result).toHaveProperty('loadedAt');
  });

  it('returns empty arrays when watchlists API returns []', async () => {
    fetchWatchlists.mockResolvedValue([]);
    const result = await loadWatchlistSymbols();
    expect(result.syms).toEqual([]);
    expect(result.pinnedSyms).toEqual([]);
    expect(result.regularSyms).toEqual([]);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// pinnedSyms / regularSyms split
// ─────────────────────────────────────────────────────────────────────────────

describe('loadWatchlistSymbols — pinnedSyms / regularSyms split', () => {
  it('puts is_pinned list symbols into pinnedSyms and regular list symbols into regularSyms', async () => {
    const lists = [
      makeList(1, { is_pinned: true }),   // pinned
      makeList(2),                         // regular
    ];
    fetchWatchlists.mockResolvedValue(lists);
    fetchWatchlist.mockImplementation(async (id) => {
      if (id === 1) return makeDetail(['NIFTY', 'BANKNIFTY']);
      if (id === 2) return makeDetail(['RELIANCE', 'TCS']);
      return makeDetail([]);
    });

    const result = await loadWatchlistSymbols();

    expect(result.pinnedSyms).toEqual(['NIFTY', 'BANKNIFTY']);
    expect(result.regularSyms).toEqual(['RELIANCE', 'TCS']);
    expect(result.syms).toEqual(['NIFTY', 'BANKNIFTY', 'RELIANCE', 'TCS']);
  });

  it('puts is_global list symbols into pinnedSyms', async () => {
    const lists = [
      makeList(10, { is_global: true }),  // global → counts as pinned
      makeList(11),                        // regular
    ];
    fetchWatchlists.mockResolvedValue(lists);
    fetchWatchlist.mockImplementation(async (id) => {
      if (id === 10) return makeDetail(['FINNIFTY']);
      if (id === 11) return makeDetail(['INFY']);
      return makeDetail([]);
    });

    const result = await loadWatchlistSymbols();

    expect(result.pinnedSyms).toContain('FINNIFTY');
    expect(result.regularSyms).toContain('INFY');
    expect(result.pinnedSyms).not.toContain('INFY');
  });

  it('deduplicates across pinned and regular — first occurrence wins', async () => {
    // NIFTY appears in both; it should land in pinnedSyms only (first seen).
    const lists = [
      makeList(1, { is_pinned: true }),
      makeList(2),
    ];
    fetchWatchlists.mockResolvedValue(lists);
    fetchWatchlist.mockImplementation(async (id) => {
      if (id === 1) return makeDetail(['NIFTY', 'BANKNIFTY']);
      if (id === 2) return makeDetail(['NIFTY', 'RELIANCE']);  // NIFTY duplicated
      return makeDetail([]);
    });

    const result = await loadWatchlistSymbols();

    expect(result.pinnedSyms).toEqual(['NIFTY', 'BANKNIFTY']);
    // NIFTY already seen → not added to regularSyms
    expect(result.regularSyms).toEqual(['RELIANCE']);
    // syms is order-preserving union
    expect(result.syms).toEqual(['NIFTY', 'BANKNIFTY', 'RELIANCE']);
  });

  it('normalises tradingsymbols to uppercase and strips surrounding whitespace', async () => {
    // loadWatchlistSymbols strips leading/trailing whitespace and uppercases.
    // Internal spaces are preserved here — the derivatives page's
    // _extractFOUnderlyingRoots does the /\s+/g strip when consuming the symbols.
    const lists = [makeList(1, { is_pinned: true })];
    fetchWatchlists.mockResolvedValue(lists);
    fetchWatchlist.mockResolvedValue(makeDetail(['nifty 50', ' BANKNIFTY ']));

    const result = await loadWatchlistSymbols();

    expect(result.pinnedSyms).toEqual(['NIFTY 50', 'BANKNIFTY']);
  });

  it('handles multiple pinned lists — both feed pinnedSyms in order', async () => {
    const lists = [
      makeList(1, { is_pinned: true }),
      makeList(2, { is_global: true }),
      makeList(3),                         // regular
    ];
    fetchWatchlists.mockResolvedValue(lists);
    fetchWatchlist.mockImplementation(async (id) => {
      if (id === 1) return makeDetail(['NIFTY']);
      if (id === 2) return makeDetail(['BANKNIFTY']);
      if (id === 3) return makeDetail(['RELIANCE']);
      return makeDetail([]);
    });

    const result = await loadWatchlistSymbols();

    expect(result.pinnedSyms).toEqual(['NIFTY', 'BANKNIFTY']);
    expect(result.regularSyms).toEqual(['RELIANCE']);
  });

  it('handles zero pinned lists — everything goes into regularSyms', async () => {
    const lists = [makeList(5), makeList(6)];
    fetchWatchlists.mockResolvedValue(lists);
    fetchWatchlist.mockImplementation(async (id) => {
      if (id === 5) return makeDetail(['HDFC']);
      if (id === 6) return makeDetail(['ICICIBANK']);
      return makeDetail([]);
    });

    const result = await loadWatchlistSymbols();

    expect(result.pinnedSyms).toEqual([]);
    expect(result.regularSyms).toEqual(['HDFC', 'ICICIBANK']);
    expect(result.syms).toEqual(['HDFC', 'ICICIBANK']);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Error / edge cases
// ─────────────────────────────────────────────────────────────────────────────

describe('loadWatchlistSymbols — error paths', () => {
  it('returns empty arrays when fetchWatchlists throws', async () => {
    fetchWatchlists.mockRejectedValue(new Error('network error'));
    const result = await loadWatchlistSymbols();
    expect(result.syms).toEqual([]);
    expect(result.pinnedSyms).toEqual([]);
    expect(result.regularSyms).toEqual([]);
  });

  it('skips a failing individual fetchWatchlist and continues', async () => {
    const lists = [makeList(1, { is_pinned: true }), makeList(2)];
    fetchWatchlists.mockResolvedValue(lists);
    fetchWatchlist.mockImplementation(async (id) => {
      if (id === 1) throw new Error('auth error');
      if (id === 2) return makeDetail(['RELIANCE']);
      return makeDetail([]);
    });

    // The per-list .catch(() => null) makes a failing fetchWatchlist yield null.
    // pinnedDetails[0] → null → skipped; regularDetails[0] → RELIANCE.
    const result = await loadWatchlistSymbols();

    expect(result.pinnedSyms).toEqual([]);
    expect(result.regularSyms).toEqual(['RELIANCE']);
  });

  it('accepts watchlists wrapped in { watchlists: [] } envelope', async () => {
    fetchWatchlists.mockResolvedValue({ watchlists: [makeList(1, { is_pinned: true })] });
    fetchWatchlist.mockResolvedValue(makeDetail(['MIDCPNIFTY']));

    const result = await loadWatchlistSymbols();

    expect(result.pinnedSyms).toEqual(['MIDCPNIFTY']);
    expect(result.regularSyms).toEqual([]);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Cache behaviour
// ─────────────────────────────────────────────────────────────────────────────

describe('loadWatchlistSymbols — cache', () => {
  it('returns cached result on second call within TTL', async () => {
    fetchWatchlists.mockResolvedValue([makeList(1, { is_pinned: true })]);
    fetchWatchlist.mockResolvedValue(makeDetail(['NIFTY']));

    await loadWatchlistSymbols();
    await loadWatchlistSymbols();  // second call

    expect(fetchWatchlists).toHaveBeenCalledTimes(1);
  });

  it('invalidateWatchlistSymbols() forces a fresh fetch on next call', async () => {
    fetchWatchlists.mockResolvedValue([makeList(1, { is_pinned: true })]);
    fetchWatchlist.mockResolvedValue(makeDetail(['NIFTY']));

    await loadWatchlistSymbols();
    invalidateWatchlistSymbols();
    await loadWatchlistSymbols();

    expect(fetchWatchlists).toHaveBeenCalledTimes(2);
  });
});
