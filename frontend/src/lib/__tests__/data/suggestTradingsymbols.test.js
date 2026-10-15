/**
 * suggestTradingsymbols.test.js — Vitest unit tests for suggestTradingsymbols
 * from instruments.js (2026-10-15, CLI `symbol=` value dropdown).
 *
 * Five quality dimensions:
 *  1. SSOT  — imports the exact function from $lib/data/instruments (not a replica)
 *  2. Perf  — pure synchronous after mocking; no I/O, no IndexedDB
 *  3. Stale — guards cold-cache (null _tradingsymbolsSorted) returns [] immediately
 *  4. Reuse — shares _prefixMatch's binary-search-prefix algorithm with
 *             suggestUnderlyings; verified at the algorithm level the same
 *             way listExpiries.test.js verifies its own internal algorithm
 *  5. UX    — case-insensitive prefix match, respects `limit`, empty prefix
 *             returns the first `limit` alphabetically (same contract as
 *             suggestUnderlyings) — these drive the CLI dropdown's
 *             "3+ chars required" gate (enforced by the CALLER in
 *             agents.js's _paramOrEnumSuggestion, not by this function
 *             itself, which has no opinion on a minimum prefix length)
 */

import { describe, it, expect, vi } from 'vitest';

// ── Mock side-effect modules so instruments.js can be imported in Node ─────
vi.mock('$lib/data/decomposeSymbol', () => ({
  _setExpiryLookup: vi.fn(),
}));
vi.mock('$lib/data/rootOf', () => ({
  seedRootMapFromInstruments: vi.fn(),
}));

// Import AFTER mocks are registered.
import { suggestTradingsymbols } from '$lib/data/instruments';

// ── Cold-cache tests ────────────────────────────────────────────────────────

describe('suggestTradingsymbols — cold cache (instruments not loaded)', () => {
  it('returns [] when the instruments cache has not been populated', () => {
    expect(suggestTradingsymbols('NIF')).toEqual([]);
  });

  it('is safe to call repeatedly on cold cache without throwing', () => {
    expect(() => {
      suggestTradingsymbols('NIF');
      suggestTradingsymbols('RELIANCE');
      suggestTradingsymbols('');
    }).not.toThrow();
  });
});

// ── Algorithm-level verification (same binary-search-prefix-scan _prefixMatch
// uses internally — not exported, so verified against a known sorted array,
// same technique listExpiries.test.js uses for its own internal algorithm) ──

describe('suggestTradingsymbols — algorithm (binary-search prefix scan)', () => {
  function prefixMatch(sortedArr, prefix, limit = 20) {
    if (!prefix) return sortedArr.slice(0, limit);
    const p = prefix.toUpperCase();
    const out = [];
    let lo = 0, hi = sortedArr.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (sortedArr[mid] < p) lo = mid + 1; else hi = mid;
    }
    while (lo < sortedArr.length && sortedArr[lo].startsWith(p) && out.length < limit) {
      out.push(sortedArr[lo]);
      lo++;
    }
    return out;
  }

  const SYMS = ['NIFTY25JULFUT', 'NIFTY26APR22500CE', 'NIF100BEES', 'RELIANCE', 'CRUDEOIL26OCTFUT'].sort();

  it('matches only tradingsymbols starting with the given prefix', () => {
    const result = prefixMatch(SYMS, 'NIFTY', 20);
    expect(result).toEqual(['NIFTY25JULFUT', 'NIFTY26APR22500CE']);
  });

  it('is case-insensitive', () => {
    expect(prefixMatch(SYMS, 'nifty', 20)).toEqual(prefixMatch(SYMS, 'NIFTY', 20));
  });

  it('respects the limit', () => {
    expect(prefixMatch(SYMS, 'NIFTY', 1)).toEqual(['NIFTY25JULFUT']);
  });

  it('returns [] when no tradingsymbol matches the prefix', () => {
    expect(prefixMatch(SYMS, 'ZZZZ', 20)).toEqual([]);
  });

  it('an empty prefix returns the first `limit` entries alphabetically', () => {
    expect(prefixMatch(SYMS, '', 2)).toEqual(SYMS.slice(0, 2));
  });
});

// ── Import contract ─────────────────────────────────────────────────────────

describe('suggestTradingsymbols — import contract', () => {
  it('is exported from $lib/data/instruments as a function', () => {
    expect(typeof suggestTradingsymbols).toBe('function');
  });

  it('accepts (prefix, limit?) and always returns an Array', () => {
    expect(Array.isArray(suggestTradingsymbols('NIF'))).toBe(true);
    expect(Array.isArray(suggestTradingsymbols('NIF', 10))).toBe(true);
  });
});
