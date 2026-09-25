/**
 * marketDataStoresMeta.test.js — coverage for marketDataStores.svelte.js's
 * `_bookStaleMeta` degraded-fetch extractor (real-money guard, 2026-09).
 *
 * marketDataStores.svelte.js has top-level `$state(...)` calls (e.g.
 * `_holdingsSnapshotAt`, `_bookPollerTick`) that execute at module-eval
 * time, so it can't be imported directly in this harness (no svelte-
 * compiler plugin in vitest.config.js). Two-part coverage instead:
 *  1. A pure-function mirror of `_bookStaleMeta`'s trivial extraction
 *     logic (`{ staleAccounts: r?.stale_accounts ?? [], asOf: r?.as_of ?? null }`).
 *  2. A source-scan (Vite `?raw` import — same convention already used by
 *     portfolioAggregatesTriggers.test.js / futuresOwnPriceValuation.test.js)
 *     confirming `meta: _bookStaleMeta` is actually wired into all five
 *     book stores (positionsStore, pulsePositionsStore, pulseHoldingsStore,
 *     holdingsStore, fundsStore) — the exact fix this session shipped.
 *
 * Five quality dimensions:
 *  1. SSOT   — source-scan reads the real shipped file, not a stale copy
 *  2. Perf   — pure function + one string read, no I/O
 *  3. Stale  — regression-guards each of the 5 stores individually; a
 *              future edit dropping `meta:` from just one store (the exact
 *              shape of bug this fix targets — partial coverage) fails
 *  4. Reuse  — mirrors the same { staleAccounts, asOf } contract shared by
 *              dataStore.svelte.js's extractStaleMeta (see dataStore.test.js)
 *  5. UX     — asOf/staleAccounts feed PositionStrip's STALE@HH:MM badge
 *              and .ps-stale tint (see PositionStrip.svelte)
 */

import { describe, it, expect } from 'vitest';
import src from '$lib/data/marketDataStores.svelte.js?raw';

// ── Pure-function mirror of _bookStaleMeta ────────────────────────────────

function bookStaleMeta(r) {
  return { staleAccounts: r?.stale_accounts ?? [], asOf: r?.as_of ?? null };
}

describe('_bookStaleMeta (pure mirror)', () => {
  it('extracts a non-empty stale_accounts list', () => {
    const meta = bookStaleMeta({ rows: [], stale_accounts: ['ZG0790', 'DH1001'], as_of: null });
    expect(meta.staleAccounts).toEqual(['ZG0790', 'DH1001']);
  });

  it('defaults to [] when stale_accounts is absent (older/unrelated response shapes)', () => {
    const meta = bookStaleMeta({ rows: [] });
    expect(meta.staleAccounts).toEqual([]);
  });

  it('defaults asOf to null when absent', () => {
    const meta = bookStaleMeta({ rows: [] });
    expect(meta.asOf).toBeNull();
  });

  it('carries through as_of for a persisted off-hours snapshot', () => {
    const meta = bookStaleMeta({ rows: [], as_of: '2026-09-25T02:30:00+00:00' });
    expect(meta.asOf).toBe('2026-09-25T02:30:00+00:00');
  });

  it('handles a null/undefined response defensively', () => {
    expect(bookStaleMeta(null)).toEqual({ staleAccounts: [], asOf: null });
    expect(bookStaleMeta(undefined)).toEqual({ staleAccounts: [], asOf: null });
  });
});

// ── Source-scan — meta: _bookStaleMeta wired into all 5 book stores ──────

describe('marketDataStores.svelte.js — meta extractor wired into every book store', () => {
  it('defines the shared _bookStaleMeta extractor', () => {
    expect(src).toMatch(/function _bookStaleMeta\(r\)\s*\{/);
    expect(src).toMatch(/staleAccounts:\s*r\?\.stale_accounts\s*\?\?\s*\[\]/);
    expect(src).toMatch(/asOf:\s*r\?\.as_of\s*\?\?\s*null/);
  });

  /**
   * Extract the createDataStore({ ... }) block for a given store name by
   * brace-matching from the declaration to its closing `});` — same
   * technique as portfolioAggregatesTriggers.test.js's extractDerivedBody.
   * @param {string} storeName
   */
  function extractStoreBlock(storeName) {
    const marker = `export const ${storeName} = createDataStore({`;
    const start = src.indexOf(marker);
    expect(start, `${storeName} declaration not found`).toBeGreaterThan(-1);
    const bodyStart = src.indexOf('{', start);
    let depth = 0;
    for (let i = bodyStart; i < src.length; i++) {
      if (src[i] === '{') depth++;
      else if (src[i] === '}') {
        depth--;
        if (depth === 0) return src.slice(bodyStart, i + 1);
      }
    }
    throw new Error(`unterminated block for ${storeName}`);
  }

  const BOOK_STORES = [
    'positionsStore',
    'pulsePositionsStore',
    'pulseHoldingsStore',
    'holdingsStore',
    'fundsStore',
  ];

  for (const name of BOOK_STORES) {
    it(`${name} passes meta: _bookStaleMeta to createDataStore`, () => {
      const block = extractStoreBlock(name);
      expect(block, `${name} must wire meta: _bookStaleMeta`).toMatch(/meta:\s*_bookStaleMeta,/);
    });
  }

  it('moversStore does NOT need the book-stale meta extractor (different response shape — no stale_accounts field)', () => {
    // moversStore's own keepStaleOnEmpty already covers its empty-guard
    // needs; PositionsResponse-style stale_accounts tagging doesn't apply
    // to the movers endpoint. Documents the intentional scope boundary so
    // a future "just add meta everywhere" edit doesn't silently misapply
    // the positions/holdings/funds contract to an unrelated response shape.
    const block = extractStoreBlock('moversStore');
    expect(block).not.toMatch(/meta:\s*_bookStaleMeta,/);
  });
});
