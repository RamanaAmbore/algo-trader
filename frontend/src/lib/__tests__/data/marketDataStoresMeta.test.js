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
import mpSrc from '$lib/MarketPulse.svelte?raw';

/**
 * Extract a named function's body source text by brace-matching from its
 * declaration (works for both `function name(` and `export function name(`
 * since indexOf matches the substring regardless of the `export ` prefix).
 * Shared across describe blocks below.
 * @param {string} name
 */
function extractFn(name) {
  const marker = `function ${name}(`;
  const start = src.indexOf(marker);
  expect(start, `${name} not found`).toBeGreaterThan(-1);
  const bodyStart = src.indexOf('{', start);
  let depth = 0;
  for (let i = bodyStart; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(bodyStart, i + 1);
    }
  }
  throw new Error(`unterminated function body for ${name}`);
}

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

// ── close_price → prev_close rename (2026-10) ─────────────────────────────
//
// PositionRow/HoldingRow dropped `close_price` in favour of `prev_close`
// (backend commit 933a9a88; see backend/api/schemas.py). The frontend
// dual-write publishers here still read the removed field, so `close`
// was published as `undefined` for every held symbol for every positions/
// holdings poll — the symbolStore merge guard silently drops a non-
// positive/undefined `close` write (see symbolStore.svelte.js's
// price-zero guard), so the bug was invisible until a consumer tried to
// read `close` back out and got nothing instead of the frozen
// prior-session settlement price.
describe('marketDataStores.svelte.js — publishers read prev_close, not the removed close_price', () => {
  it('_publishPositionsRows writes close: r.prev_close', () => {
    const block = extractFn('_publishPositionsRows');
    expect(block).toMatch(/close:\s*r\.prev_close,/);
    expect(block).not.toMatch(/close:\s*r\.close_price,/);
  });

  it('_publishHoldingsRows writes close: r.prev_close', () => {
    const block = extractFn('_publishHoldingsRows');
    expect(block).toMatch(/close:\s*r\.prev_close,/);
    expect(block).not.toMatch(/close:\s*r\.close_price,/);
  });
});

// ── GOLD prev_close source race (item 1, 2026-10) ─────────────────────────
//
// Two publishers raced to set a symbol's `close` in symbolStore:
// _publishPositionsRows (daily_book.ltp basis, correct) vs
// publishPulseQuotes (Kite ohlc.close basis, banned). Both stamp
// snapshot_ts: Date.now() with no source-priority, so whichever call
// landed last won — a held GOLD position's `close` could silently flip to
// the wrong basis. Fix: publishPulseQuotes now accepts an optional
// `{ skipCloseSymbols }` opts bag; MarketPulse.svelte builds this set from
// the current poll's held positions (prev_close > 0) and passes it in, so
// `close` is omitted entirely for those symbols — the positions publisher
// stays sole authority.
//
// Pure-mirror test (module has top-level $state, can't import directly —
// same constraint documented at the top of this file) + a source-scan
// confirming the real implementation wires the same contract.

/** Pure mirror of publishPulseQuotes' per-item field-building logic. */
function buildPulseQuoteFields(q, skipCloseSymbols) {
  const sym = String(q?.tradingsymbol || '').toUpperCase();
  return {
    sym,
    close: (skipCloseSymbols && skipCloseSymbols.has(sym)) ? undefined : q.close,
  };
}

describe('publishPulseQuotes — skipCloseSymbols omits close for held positions (item 1)', () => {
  it('omits close for a symbol in the skip set (held position with valid prev_close)', () => {
    const skip = new Set(['GOLDM26SEPFUT']);
    const { fields } = { fields: buildPulseQuoteFields({ tradingsymbol: 'GOLDM26SEPFUT', close: 71000 }, skip) };
    expect(fields.close).toBeUndefined();
  });

  it('still writes close for a symbol NOT in the skip set (no position, legitimate Kite fallback)', () => {
    const skip = new Set(['GOLDM26SEPFUT']);
    const { fields } = { fields: buildPulseQuoteFields({ tradingsymbol: 'NIFTY', close: 24000 }, skip) };
    expect(fields.close).toBe(24000);
  });

  it('writes close normally when skipCloseSymbols is undefined (no held positions this poll)', () => {
    const { fields } = { fields: buildPulseQuoteFields({ tradingsymbol: 'GOLDM26SEPFUT', close: 71000 }, undefined) };
    expect(fields.close).toBe(71000);
  });

  it('an undefined close field is a safe no-op when merged (mirrors _mergeSymbolWrite\'s raw == null skip)', () => {
    const skip = new Set(['GOLDM26SEPFUT']);
    const { fields } = { fields: buildPulseQuoteFields({ tradingsymbol: 'GOLDM26SEPFUT', close: 71000 }, skip) };
    // _mergeSymbolWrite does `for (const [k, raw] of Object.entries(fields)) { if (raw == null) continue; }`
    // — undefined satisfies `== null`, so this field is dropped entirely,
    // never overwriting whatever _publishPositionsRows already wrote.
    expect(fields.close == null).toBe(true);
  });
});

describe('marketDataStores.svelte.js — publishPulseQuotes skipCloseSymbols wiring (source scan)', () => {
  it('publishPulseQuotes accepts an opts bag and reads skipCloseSymbols', () => {
    const block = extractFn('publishPulseQuotes');
    expect(block).toMatch(/opts\?\.skipCloseSymbols instanceof Set/);
  });

  it('close field is conditionally omitted for symbols in the skip set', () => {
    const block = extractFn('publishPulseQuotes');
    expect(block).toMatch(/close:\s*\(skipClose && skipClose\.has\(sym\)\)\s*\?\s*undefined\s*:\s*q\.close,/);
  });
});

describe('MarketPulse.svelte — publishPulseQuotes call site builds the held-symbol skip set', () => {
  it('passes skipCloseSymbols built from p_rows with prev_close > 0', () => {
    expect(mpSrc).toMatch(/_skipCloseSymbols\.add\(_s\)/);
    expect(mpSrc).toMatch(/publishPulseQuotes\(items,\s*\{\s*skipCloseSymbols:\s*_skipCloseSymbols\s*\}\)/);
  });
});
