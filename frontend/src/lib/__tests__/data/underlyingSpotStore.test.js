/**
 * underlyingSpotStore.test.js
 *
 * Unit tests for the underlyingSpotStore logic.
 *
 * The store:
 *   1. Holds { ROOT: { ltp, day_pct, prev_close } } populated by batchQuote
 *   2. getUnderlyingSpot(root) returns ltp or 0 when root absent
 *   3. loadUnderlyingSpots(pairs) calls batchQuote, builds the map, calls publishPulseQuotes
 *   4. patchUnderlyingSpot(root, ltp) applies per-tick LTP patches without a full reload
 *
 * NOTE: The store uses Svelte 5 $state runes and cannot be imported directly
 * into Vitest. Tests use a local helper that mirrors the store's computation
 * logic — the same pattern used in positionsDayPnlStore.test.js and
 * holdingsDayPnlStore.test.js.
 *
 * Five quality dimensions:
 *   1. SSOT   — helpers mirror underlyingSpotStore's loadUnderlyingSpots logic exactly
 *   2. Perf   — pure unit, no DOM, sub-millisecond
 *   3. Stale  — unknown root returns 0; missing batchQuote items handled gracefully
 *   4. Reuse  — applyUnderlyingTickLtp from underlyingQuoteUtils is the canonical patch fn
 *   5. UX     — MCX CRUDEOIL (futures quoteKey) and NSE index (spot quoteKey) both handled
 */

import { describe, it, expect, vi } from 'vitest';
import { applyUnderlyingTickLtp } from '$lib/data/underlyingQuoteUtils.js';

// ── Local mirror of store's loadUnderlyingSpots logic ──────────────────────
// Mirrors the batchQuote-to-map building logic in underlyingSpotStore.svelte.js.
// Does NOT call batchQuote (passed in as a stub); does NOT call publishPulseQuotes.

/**
 * @param {Array<{ root: string, quoteKey: string }>} pairs
 * @param {any} batchQuoteResult - { items: [...] } mock response
 * @returns {Record<string, { ltp: number, day_pct: number | null, prev_close: number }>}
 */
function buildQuoteMap(pairs, batchQuoteResult) {
  const items = batchQuoteResult?.items ?? [];

  /** @type {Record<string, any>} */
  const byKey = {};
  for (const it of items) {
    if (!it?.exchange || !it?.tradingsymbol) continue;
    byKey[`${it.exchange}:${it.tradingsymbol}`] = it;
  }

  /** @type {Record<string, { ltp: number, day_pct: number | null, prev_close: number }>} */
  const next = {};
  for (const { root, quoteKey } of pairs) {
    const q = byKey[quoteKey];
    if (!q) continue;
    const ltp   = Number(q.ltp   ?? q.last_price ?? 0);
    const close = Number(q.close ?? q.ohlc?.close ?? 0);
    let pct = null;
    if (q.change_pct != null)          pct = Number(q.change_pct);
    else if (q.change_percent != null) pct = Number(q.change_percent);
    else if (close > 0 && ltp > 0)    pct = ((ltp - close) / close) * 100;
    next[root] = { ltp, day_pct: pct, prev_close: close };
  }
  return next;
}

/** Mirror of getUnderlyingSpot */
function getSpot(quotes, root) {
  return quotes[root]?.ltp ?? 0;
}

// ── Test 1: empty state — unknown root returns 0 ────────────────────────────

describe('underlyingSpotStore — initial/empty state', () => {
  it('unknown root returns 0 before any load', () => {
    const quotes = {};
    expect(getSpot(quotes, 'CRUDEOIL')).toBe(0);
    expect(getSpot(quotes, 'NIFTY')).toBe(0);
    expect(getSpot(quotes, 'UNKNOWN')).toBe(0);
  });

  it('empty pairs array produces empty map', () => {
    const result = buildQuoteMap([], { items: [] });
    expect(result).toEqual({});
  });
});

// ── Test 2: MCX CRUDEOIL futures quoteKey ────────────────────────────────────

describe('underlyingSpotStore — MCX CRUDEOIL after loadUnderlyingSpots', () => {
  const pairs = [{ root: 'CRUDEOIL', quoteKey: 'MCX:CRUDEOIL26SEPFUT' }];
  const batchQuoteResult = {
    items: [
      {
        exchange: 'MCX',
        tradingsymbol: 'CRUDEOIL26SEPFUT',
        ltp: 5788,
        close: 5700,
        change_pct: null,
        change_percent: null,
      },
    ],
  };

  it('getUnderlyingSpot returns correct ltp after load', () => {
    const quotes = buildQuoteMap(pairs, batchQuoteResult);
    expect(getSpot(quotes, 'CRUDEOIL')).toBe(5788);
  });

  it('underlyingQuotes["CRUDEOIL"] has ltp, prev_close, day_pct', () => {
    const quotes = buildQuoteMap(pairs, batchQuoteResult);
    const entry = quotes['CRUDEOIL'];
    expect(entry).toBeDefined();
    expect(entry.ltp).toBe(5788);
    expect(entry.prev_close).toBe(5700);
    // day_pct computed from (5788 - 5700) / 5700 * 100 ≈ 1.544
    expect(entry.day_pct).toBeCloseTo((5788 - 5700) / 5700 * 100, 3);
  });

  it('getUnderlyingSpot returns 0 for unloaded root even after another root is loaded', () => {
    const quotes = buildQuoteMap(pairs, batchQuoteResult);
    expect(getSpot(quotes, 'GOLD')).toBe(0);
    expect(getSpot(quotes, 'NIFTY')).toBe(0);
  });
});

// ── Test 3: change_pct field takes priority over computed pct ───────────────

describe('underlyingSpotStore — day_pct source priority', () => {
  it('uses change_pct when available', () => {
    const pairs = [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }];
    const result = buildQuoteMap(pairs, {
      items: [{ exchange: 'NSE', tradingsymbol: 'NIFTY 50', ltp: 24000, close: 23800, change_pct: 0.84 }],
    });
    expect(result['NIFTY'].day_pct).toBe(0.84);
  });

  it('uses change_percent as second fallback when change_pct is absent', () => {
    const pairs = [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }];
    const result = buildQuoteMap(pairs, {
      items: [{ exchange: 'NSE', tradingsymbol: 'NIFTY 50', ltp: 24000, close: 23800, change_percent: 0.84 }],
    });
    expect(result['NIFTY'].day_pct).toBe(0.84);
  });

  it('computes pct from ltp and close when both change fields are absent', () => {
    const pairs = [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }];
    const result = buildQuoteMap(pairs, {
      items: [{ exchange: 'NSE', tradingsymbol: 'NIFTY 50', ltp: 24200, close: 24000 }],
    });
    // (24200 - 24000) / 24000 * 100 = 0.833...
    expect(result['NIFTY'].day_pct).toBeCloseTo((24200 - 24000) / 24000 * 100, 3);
  });

  it('day_pct is null when close is 0', () => {
    const pairs = [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }];
    const result = buildQuoteMap(pairs, {
      items: [{ exchange: 'NSE', tradingsymbol: 'NIFTY 50', ltp: 24000, close: 0 }],
    });
    expect(result['NIFTY'].day_pct).toBeNull();
  });
});

// ── Test 4: multi-underlying load ───────────────────────────────────────────

describe('underlyingSpotStore — multi-underlying load', () => {
  const pairs = [
    { root: 'CRUDEOIL', quoteKey: 'MCX:CRUDEOIL26SEPFUT' },
    { root: 'NIFTY',    quoteKey: 'NSE:NIFTY 50' },
    { root: 'GOLD',     quoteKey: 'MCX:GOLD26OCTFUT' },
  ];
  const batchQuoteResult = {
    items: [
      { exchange: 'MCX', tradingsymbol: 'CRUDEOIL26SEPFUT', ltp: 5788, close: 5700 },
      { exchange: 'NSE', tradingsymbol: 'NIFTY 50',         ltp: 24200, close: 24000 },
      { exchange: 'MCX', tradingsymbol: 'GOLD26OCTFUT',     ltp: 74000, close: 73500 },
    ],
  };

  it('all three roots populated correctly', () => {
    const quotes = buildQuoteMap(pairs, batchQuoteResult);
    expect(getSpot(quotes, 'CRUDEOIL')).toBe(5788);
    expect(getSpot(quotes, 'NIFTY')).toBe(24200);
    expect(getSpot(quotes, 'GOLD')).toBe(74000);
  });

  it('prev_close is correct for each root', () => {
    const quotes = buildQuoteMap(pairs, batchQuoteResult);
    expect(quotes['CRUDEOIL'].prev_close).toBe(5700);
    expect(quotes['NIFTY'].prev_close).toBe(24000);
    expect(quotes['GOLD'].prev_close).toBe(73500);
  });
});

// ── Test 5: missing batchQuote items handled gracefully ─────────────────────

describe('underlyingSpotStore — missing or partial batchQuote response', () => {
  it('root is absent in result when batchQuote returns no item for it', () => {
    const pairs = [
      { root: 'CRUDEOIL', quoteKey: 'MCX:CRUDEOIL26SEPFUT' },
      { root: 'NIFTY',    quoteKey: 'NSE:NIFTY 50' },
    ];
    // batchQuote only returns NIFTY, not CRUDEOIL
    const result = buildQuoteMap(pairs, {
      items: [{ exchange: 'NSE', tradingsymbol: 'NIFTY 50', ltp: 24200, close: 24000 }],
    });
    expect(result['NIFTY'].ltp).toBe(24200);
    expect(result['CRUDEOIL']).toBeUndefined();
    expect(getSpot(result, 'CRUDEOIL')).toBe(0);
  });

  it('empty items array produces empty map', () => {
    const pairs = [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }];
    const result = buildQuoteMap(pairs, { items: [] });
    expect(result).toEqual({});
  });

  it('item with missing exchange/tradingsymbol is skipped', () => {
    const pairs = [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }];
    const result = buildQuoteMap(pairs, {
      items: [
        { tradingsymbol: 'NIFTY 50', ltp: 24000, close: 23800 },  // missing exchange
        { exchange: 'NSE', ltp: 24000, close: 23800 },              // missing tradingsymbol
      ],
    });
    expect(result['NIFTY']).toBeUndefined();
  });
});

// ── Test 6: patchUnderlyingSpot via applyUnderlyingTickLtp ──────────────────
// patchUnderlyingSpot delegates to applyUnderlyingTickLtp — test that utility
// directly since the store itself cannot be imported in Vitest (Svelte 5 runes).

describe('underlyingSpotStore — tick patch logic (via applyUnderlyingTickLtp)', () => {
  const BASE = {
    CRUDEOIL: { ltp: 5788, day_pct: 1.5, prev_close: 5700 },
    NIFTY:    { ltp: 24200, day_pct: 0.8, prev_close: 24000 },
  };

  it('patch returns updated map when ltp changes', () => {
    const result = applyUnderlyingTickLtp(BASE, 'CRUDEOIL', 5800);
    expect(result.CRUDEOIL.ltp).toBe(5800);
    expect(result).not.toBe(BASE);        // new object created
  });

  it('patch preserves day_pct and prev_close', () => {
    const result = applyUnderlyingTickLtp(BASE, 'CRUDEOIL', 5800);
    expect(result.CRUDEOIL.day_pct).toBe(1.5);
    expect(result.CRUDEOIL.prev_close).toBe(5700);
  });

  it('patch returns same reference when ltp identical (no-op guard)', () => {
    const result = applyUnderlyingTickLtp(BASE, 'CRUDEOIL', 5788);
    expect(result).toBe(BASE);
  });

  it('patch returns same reference when root is not in map (no phantom entry)', () => {
    const result = applyUnderlyingTickLtp(BASE, 'GOLD', 74000);
    expect(result).toBe(BASE);
  });

  it('patch returns same reference when ltp is null', () => {
    const result = applyUnderlyingTickLtp(BASE, 'CRUDEOIL', null);
    expect(result).toBe(BASE);
  });

  it('patch returns same reference when ltp is 0', () => {
    const result = applyUnderlyingTickLtp(BASE, 'CRUDEOIL', 0);
    expect(result).toBe(BASE);
  });

  it('only patched root changes — sibling root untouched', () => {
    const result = applyUnderlyingTickLtp(BASE, 'CRUDEOIL', 5900);
    expect(result.NIFTY).toBe(BASE.NIFTY);   // same nested reference
    expect(result.NIFTY.ltp).toBe(24200);
  });
});

// ── Test 7: last_price fallback when ltp field is absent ────────────────────

describe('underlyingSpotStore — last_price fallback', () => {
  it('uses last_price when ltp is absent', () => {
    const pairs = [{ root: 'CRUDEOIL', quoteKey: 'MCX:CRUDEOIL26SEPFUT' }];
    const result = buildQuoteMap(pairs, {
      items: [{ exchange: 'MCX', tradingsymbol: 'CRUDEOIL26SEPFUT', last_price: 5788, close: 5700 }],
    });
    expect(result['CRUDEOIL'].ltp).toBe(5788);
  });

  it('uses ohlc.close when close field is absent', () => {
    const pairs = [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }];
    const result = buildQuoteMap(pairs, {
      items: [{ exchange: 'NSE', tradingsymbol: 'NIFTY 50', ltp: 24200, ohlc: { close: 24000 } }],
    });
    expect(result['NIFTY'].prev_close).toBe(24000);
  });
});
