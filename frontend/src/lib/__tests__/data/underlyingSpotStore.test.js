/**
 * underlyingSpotStore.test.js
 *
 * Unit tests for the underlyingSpotStore logic.
 *
 * The store:
 *   1. Holds { ROOT: { ltp, day_pct, prev_close } } populated by batchQuote
 *   2. getUnderlyingSpot(root) returns ltp or 0 when root absent (store-level
 *      behaviour tested here; the front-month resolution wrapper added on
 *      top of it lives in underlyingSpotStore.svelte.js and can't be
 *      imported directly — see resolveUnderlying.test.js for the resolver
 *      itself)
 *   3. loadUnderlyingSpots(pairs) calls batchQuote, merges via
 *      buildUnderlyingQuoteUpdate, calls publishPulseQuotes
 *   4. patchUnderlyingSpot(root, ltp) applies per-tick LTP patches without
 *      a full reload, via applyUnderlyingTickLtp
 *
 * NOTE: The store itself uses Svelte 5 $state runes and cannot be imported
 * directly into Vitest. This file imports the REAL pure helpers from
 * underlyingQuoteUtils.js — `buildUnderlyingQuoteUpdate` (the merge logic
 * loadUnderlyingSpots delegates to) and `applyUnderlyingTickLtp` (the patch
 * logic patchUnderlyingSpot delegates to) — rather than a hand-mirrored
 * local reimplementation, closing a "tests a mirror, not the real code" gap.
 *
 * Five quality dimensions:
 *   1. SSOT   — imports buildUnderlyingQuoteUpdate/applyUnderlyingTickLtp
 *               directly from underlyingQuoteUtils.js; no reimplementation.
 *   2. Perf   — pure unit, no DOM, sub-millisecond
 *   3. Stale  — unknown root returns 0; missing batchQuote items handled
 *               gracefully; zero-value and stale-response races guarded
 *   4. Reuse  — applyUnderlyingTickLtp / buildUnderlyingQuoteUpdate are the
 *               canonical merge+patch functions, shared by PositionStrip,
 *               the derivatives page, and portfolioStore
 *   5. UX     — MCX CRUDEOIL (futures quoteKey) and NSE index (spot
 *               quoteKey) both handled; a race must never blank a cell
 *               that was showing a real price
 */

import { describe, it, expect } from 'vitest';
import { applyUnderlyingTickLtp, buildUnderlyingQuoteUpdate } from '$lib/data/underlyingQuoteUtils.js';

/** Mirror of getUnderlyingSpot's store-level lookup (post front-month resolution). */
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
    const result = buildUnderlyingQuoteUpdate([], [], {}, {}, 0);
    expect(result).toEqual({});
  });
});

// ── Test 2: MCX CRUDEOIL futures quoteKey ────────────────────────────────────

describe('underlyingSpotStore — MCX CRUDEOIL after loadUnderlyingSpots', () => {
  const pairs = [{ root: 'CRUDEOIL', quoteKey: 'MCX:CRUDEOIL26SEPFUT' }];
  const items = [
    {
      exchange: 'MCX',
      tradingsymbol: 'CRUDEOIL26SEPFUT',
      ltp: 5788,
      close: 5700,
      change_pct: null,
      change_percent: null,
    },
  ];

  it('getUnderlyingSpot returns correct ltp after load', () => {
    const quotes = buildUnderlyingQuoteUpdate(pairs, items, {}, {}, 0);
    expect(getSpot(quotes, 'CRUDEOIL')).toBe(5788);
  });

  it('underlyingQuotes["CRUDEOIL"] has ltp, prev_close, day_pct', () => {
    const quotes = buildUnderlyingQuoteUpdate(pairs, items, {}, {}, 0);
    const entry = quotes['CRUDEOIL'];
    expect(entry).toBeDefined();
    expect(entry.ltp).toBe(5788);
    expect(entry.prev_close).toBe(5700);
    // day_pct computed from (5788 - 5700) / 5700 * 100 ≈ 1.544
    expect(entry.day_pct).toBeCloseTo((5788 - 5700) / 5700 * 100, 3);
  });

  it('getUnderlyingSpot returns 0 for unloaded root even after another root is loaded', () => {
    const quotes = buildUnderlyingQuoteUpdate(pairs, items, {}, {}, 0);
    expect(getSpot(quotes, 'GOLD')).toBe(0);
    expect(getSpot(quotes, 'NIFTY')).toBe(0);
  });
});

// ── Test 3: change_pct field takes priority over computed pct ───────────────

describe('underlyingSpotStore — day_pct source priority', () => {
  it('uses change_pct when available', () => {
    const pairs = [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }];
    const result = buildUnderlyingQuoteUpdate(
      pairs,
      [{ exchange: 'NSE', tradingsymbol: 'NIFTY 50', ltp: 24000, close: 23800, change_pct: 0.84 }],
      {}, {}, 0,
    );
    expect(result['NIFTY'].day_pct).toBe(0.84);
  });

  it('uses change_percent as second fallback when change_pct is absent', () => {
    const pairs = [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }];
    const result = buildUnderlyingQuoteUpdate(
      pairs,
      [{ exchange: 'NSE', tradingsymbol: 'NIFTY 50', ltp: 24000, close: 23800, change_percent: 0.84 }],
      {}, {}, 0,
    );
    expect(result['NIFTY'].day_pct).toBe(0.84);
  });

  it('computes pct from ltp and close when both change fields are absent', () => {
    const pairs = [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }];
    const result = buildUnderlyingQuoteUpdate(
      pairs,
      [{ exchange: 'NSE', tradingsymbol: 'NIFTY 50', ltp: 24200, close: 24000 }],
      {}, {}, 0,
    );
    // (24200 - 24000) / 24000 * 100 = 0.833...
    expect(result['NIFTY'].day_pct).toBeCloseTo((24200 - 24000) / 24000 * 100, 3);
  });

  it('day_pct is null when close is 0', () => {
    const pairs = [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }];
    const result = buildUnderlyingQuoteUpdate(
      pairs,
      [{ exchange: 'NSE', tradingsymbol: 'NIFTY 50', ltp: 24000, close: 0 }],
      {}, {}, 0,
    );
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
  const items = [
    { exchange: 'MCX', tradingsymbol: 'CRUDEOIL26SEPFUT', ltp: 5788, close: 5700 },
    { exchange: 'NSE', tradingsymbol: 'NIFTY 50',         ltp: 24200, close: 24000 },
    { exchange: 'MCX', tradingsymbol: 'GOLD26OCTFUT',     ltp: 74000, close: 73500 },
  ];

  it('all three roots populated correctly', () => {
    const quotes = buildUnderlyingQuoteUpdate(pairs, items, {}, {}, 0);
    expect(getSpot(quotes, 'CRUDEOIL')).toBe(5788);
    expect(getSpot(quotes, 'NIFTY')).toBe(24200);
    expect(getSpot(quotes, 'GOLD')).toBe(74000);
  });

  it('prev_close is correct for each root', () => {
    const quotes = buildUnderlyingQuoteUpdate(pairs, items, {}, {}, 0);
    expect(quotes['CRUDEOIL'].prev_close).toBe(5700);
    expect(quotes['NIFTY'].prev_close).toBe(24000);
    expect(quotes['GOLD'].prev_close).toBe(73500);
  });
});

// ── Test 4b: regression — two independent callers must not wipe each other ───

describe('underlyingSpotStore — regression: sequential loadUnderlyingSpots merges, not replaces', () => {
  it('second caller does not wipe first caller symbols after second loadUnderlyingSpots', () => {
    // Regression test: loadUnderlyingSpots should MERGE into the store, not
    // replace. Both PositionStrip and the derivatives page call
    // loadUnderlyingSpots with different symbol sets; the second call must
    // preserve the first call's entries. buildUnderlyingQuoteUpdate now
    // performs this merge internally (spreads prevQuotes into the return),
    // so passing the previous result back in as prevQuotes is the real
    // calling convention loadUnderlyingSpots uses.
    let merged = buildUnderlyingQuoteUpdate(
      [{ root: 'GOLDM', quoteKey: 'MCX:GOLDM24OCTFUT' }],
      [{ exchange: 'MCX', tradingsymbol: 'GOLDM24OCTFUT', ltp: 72500, close: 72200 }],
      {}, {}, 0,
    );
    expect(merged['GOLDM'].ltp).toBe(72500);

    merged = buildUnderlyingQuoteUpdate(
      [{ root: 'CRUDEOIL', quoteKey: 'MCX:CRUDEOIL24OCTFUT' }],
      [{ exchange: 'MCX', tradingsymbol: 'CRUDEOIL24OCTFUT', ltp: 5900, close: 5850 }],
      merged, {}, 0,
    );

    expect(merged['GOLDM'].ltp).toBe(72500);
    expect(merged['CRUDEOIL'].ltp).toBe(5900);
    expect(Object.keys(merged).length).toBe(2);
  });

  it('third unrelated call preserves both previous entries', () => {
    /** @type {Record<string, {ltp: number, day_pct: number|null, prev_close: number}>} */
    let merged = {};

    merged = buildUnderlyingQuoteUpdate(
      [{ root: 'GOLDM', quoteKey: 'MCX:GOLDM24OCTFUT' }],
      [{ exchange: 'MCX', tradingsymbol: 'GOLDM24OCTFUT', ltp: 72500, close: 72200 }],
      merged, {}, 0,
    );
    merged = buildUnderlyingQuoteUpdate(
      [{ root: 'CRUDEOIL', quoteKey: 'MCX:CRUDEOIL24OCTFUT' }],
      [{ exchange: 'MCX', tradingsymbol: 'CRUDEOIL24OCTFUT', ltp: 5900, close: 5850 }],
      merged, {}, 0,
    );
    merged = buildUnderlyingQuoteUpdate(
      [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }],
      [{ exchange: 'NSE', tradingsymbol: 'NIFTY 50', ltp: 24200, close: 24000 }],
      merged, {}, 0,
    );

    expect(merged['GOLDM'].ltp).toBe(72500);
    expect(merged['CRUDEOIL'].ltp).toBe(5900);
    expect(merged['NIFTY'].ltp).toBe(24200);
    expect(Object.keys(merged).length).toBe(3);
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
    const result = buildUnderlyingQuoteUpdate(
      pairs,
      [{ exchange: 'NSE', tradingsymbol: 'NIFTY 50', ltp: 24200, close: 24000 }],
      {}, {}, 0,
    );
    expect(result['NIFTY'].ltp).toBe(24200);
    expect(result['CRUDEOIL']).toBeUndefined();
    expect(getSpot(result, 'CRUDEOIL')).toBe(0);
  });

  it('empty items array produces empty map', () => {
    const pairs = [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }];
    const result = buildUnderlyingQuoteUpdate(pairs, [], {}, {}, 0);
    expect(result).toEqual({});
  });

  it('item with missing exchange/tradingsymbol is skipped', () => {
    const pairs = [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }];
    const result = buildUnderlyingQuoteUpdate(
      pairs,
      [
        { tradingsymbol: 'NIFTY 50', ltp: 24000, close: 23800 },  // missing exchange
        { exchange: 'NSE', ltp: 24000, close: 23800 },              // missing tradingsymbol
      ],
      {}, {}, 0,
    );
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
    const result = buildUnderlyingQuoteUpdate(
      pairs,
      [{ exchange: 'MCX', tradingsymbol: 'CRUDEOIL26SEPFUT', last_price: 5788, close: 5700 }],
      {}, {}, 0,
    );
    expect(result['CRUDEOIL'].ltp).toBe(5788);
  });

  it('uses ohlc.close when close field is absent', () => {
    const pairs = [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }];
    const result = buildUnderlyingQuoteUpdate(
      pairs,
      [{ exchange: 'NSE', tradingsymbol: 'NIFTY 50', ltp: 24200, ohlc: { close: 24000 } }],
      {}, {}, 0,
    );
    expect(result['NIFTY'].prev_close).toBe(24000);
  });
});

// ── Test 8: zero-value guard — missing/zero broker row must not clobber a good value ──

describe('buildUnderlyingQuoteUpdate — zero-value guard', () => {
  it('a zero ltp in the response does not clobber a previously-good ltp', () => {
    const prevQuotes = { CRUDEOIL: { ltp: 5788, day_pct: 1.0, prev_close: 5730 } };
    const result = buildUnderlyingQuoteUpdate(
      [{ root: 'CRUDEOIL', quoteKey: 'MCX:CRUDEOIL26SEPFUT' }],
      [{ exchange: 'MCX', tradingsymbol: 'CRUDEOIL26SEPFUT', ltp: 0, close: 5730 }],
      prevQuotes, {}, 0,
    );
    expect(result.CRUDEOIL.ltp).toBe(5788); // kept, not clobbered by 0
  });

  it('a zero ltp with NO previous value writes through as 0 (cold-start entry-exists behaviour)', () => {
    const result = buildUnderlyingQuoteUpdate(
      [{ root: 'GOLDM', quoteKey: 'MCX:GOLDM26OCTFUT' }],
      [{ exchange: 'MCX', tradingsymbol: 'GOLDM26OCTFUT', ltp: 0, close: 72000 }],
      {}, {}, 0,
    );
    // No prior good value to protect — entry must still exist (root in
    // quotes) so later patchUnderlyingSpot ticks can apply.
    expect(result.GOLDM).toBeDefined();
    expect(result.GOLDM.ltp).toBe(0);
  });

  it('a zero close (prev_close) in the response does not clobber a previously-good prev_close', () => {
    const prevQuotes = { NIFTY: { ltp: 24000, day_pct: 0.5, prev_close: 23880 } };
    const result = buildUnderlyingQuoteUpdate(
      [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }],
      [{ exchange: 'NSE', tradingsymbol: 'NIFTY 50', ltp: 24100, close: 0 }],
      prevQuotes, {}, 0,
    );
    expect(result.NIFTY.prev_close).toBe(23880); // kept, not clobbered by 0
    expect(result.NIFTY.ltp).toBe(24100); // fresh ltp still applied normally
  });

  it('day_pct is recomputed from the KEPT ltp, not the discarded response ltp, when the zero-guard fires', () => {
    const prevQuotes = { CRUDEOIL: { ltp: 5788, day_pct: 1.0, prev_close: 5700 } };
    const result = buildUnderlyingQuoteUpdate(
      [{ root: 'CRUDEOIL', quoteKey: 'MCX:CRUDEOIL26SEPFUT' }],
      // Response carries a misleading change_pct computed against its own
      // (discarded) ltp=0 — must not be used.
      [{ exchange: 'MCX', tradingsymbol: 'CRUDEOIL26SEPFUT', ltp: 0, close: 5750, change_pct: -100 }],
      prevQuotes, {}, 0,
    );
    expect(result.CRUDEOIL.ltp).toBe(5788); // kept
    expect(result.CRUDEOIL.prev_close).toBe(5750); // close still updates
    // Recomputed from kept ltp (5788) vs new close (5750), NOT -100.
    expect(result.CRUDEOIL.day_pct).toBeCloseTo((5788 - 5750) / 5750 * 100, 3);
  });
});

// ── Test 9: per-root ordering guard — a stale response must not overwrite a newer tick ──

describe('buildUnderlyingQuoteUpdate — per-root ordering (race) guard', () => {
  it('a response whose request predates a later-applied tick discards the response ltp, keeps the tick ltp', () => {
    const reqStartedAt = 1_000;
    const tickAppliedAt = 2_000; // tick landed AFTER the request started
    const prevQuotes = { CRUDEOIL: { ltp: 5900, day_pct: 2.0, prev_close: 5700 } }; // tick already applied this value
    const lastTickAt = { CRUDEOIL: tickAppliedAt };

    const result = buildUnderlyingQuoteUpdate(
      [{ root: 'CRUDEOIL', quoteKey: 'MCX:CRUDEOIL26SEPFUT' }],
      // Stale response reflects the OLDER price, quoted before the request started.
      [{ exchange: 'MCX', tradingsymbol: 'CRUDEOIL26SEPFUT', ltp: 5788, close: 5700 }],
      prevQuotes, lastTickAt, reqStartedAt,
    );
    // The newer tick's ltp must win — the stale response's ltp is discarded.
    expect(result.CRUDEOIL.ltp).toBe(5900);
  });

  it('a stale response still updates prev_close/day_pct even though its ltp is discarded', () => {
    const reqStartedAt = 1_000;
    const tickAppliedAt = 2_000;
    const prevQuotes = { CRUDEOIL: { ltp: 5900, day_pct: 2.0, prev_close: 5700 } };
    const lastTickAt = { CRUDEOIL: tickAppliedAt };

    const result = buildUnderlyingQuoteUpdate(
      [{ root: 'CRUDEOIL', quoteKey: 'MCX:CRUDEOIL26SEPFUT' }],
      [{ exchange: 'MCX', tradingsymbol: 'CRUDEOIL26SEPFUT', ltp: 5788, close: 5750 }],
      prevQuotes, lastTickAt, reqStartedAt,
    );
    expect(result.CRUDEOIL.ltp).toBe(5900);          // kept (tick wins)
    expect(result.CRUDEOIL.prev_close).toBe(5750);   // close still refreshed from response
    // day_pct recomputed from kept ltp (5900) vs new close (5750).
    expect(result.CRUDEOIL.day_pct).toBeCloseTo((5900 - 5750) / 5750 * 100, 3);
  });

  it('a response whose request STARTED AFTER the last tick (normal case) applies normally', () => {
    const reqStartedAt = 3_000;
    const tickAppliedAt = 1_000; // tick landed BEFORE this request started — response is fresher
    const prevQuotes = { CRUDEOIL: { ltp: 5788, day_pct: 1.0, prev_close: 5700 } };
    const lastTickAt = { CRUDEOIL: tickAppliedAt };

    const result = buildUnderlyingQuoteUpdate(
      [{ root: 'CRUDEOIL', quoteKey: 'MCX:CRUDEOIL26SEPFUT' }],
      [{ exchange: 'MCX', tradingsymbol: 'CRUDEOIL26SEPFUT', ltp: 5950, close: 5700 }],
      prevQuotes, lastTickAt, reqStartedAt,
    );
    expect(result.CRUDEOIL.ltp).toBe(5950); // response applies — it's the freshest data
  });

  it('no lastTickAt entry for the root (never ticked) — response always applies', () => {
    const result = buildUnderlyingQuoteUpdate(
      [{ root: 'NIFTY', quoteKey: 'NSE:NIFTY 50' }],
      [{ exchange: 'NSE', tradingsymbol: 'NIFTY 50', ltp: 24200, close: 24000 }],
      {}, {}, 5_000,
    );
    expect(result.NIFTY.ltp).toBe(24200);
  });
});
