/**
 * resolveUnderlying.test.js — Vitest unit tests for resolveUnderlying.js
 *
 * Five quality dimensions:
 *  1. SSOT  — exercises the exported sets and resolveUnderlying() that all
 *             chart and order surfaces rely on for exchange routing.
 *  2. Perf  — pure data — no I/O; all tests complete in < 1 ms.
 *  3. Stale — guards that MCX_COMMODITIES / CDS_CURRENCIES stay in sync with
 *             backend MCX_VIRTUAL_ROOTS / CDS_VIRTUAL_ROOTS so discontinued or
 *             missing contracts are caught at test time.
 *  4. Reuse — resolveUnderlying() is the single routing boundary used by
 *             ChartWorkspace, OrderTicket, and DerivativesPage.
 *  5. UX    — correct exchange routing prevents chart empty-bars and wrong
 *             historical API responses for CDS and MCX symbols.
 */

import { describe, it, expect, afterEach } from 'vitest';
import {
  MCX_COMMODITIES,
  CDS_CURRENCIES,
  KITE_INDEX_QUOTE_KEY_TO_ROOT,
  INDEX_LTP_KEY,
  resolveUnderlying,
  resolveAnchorToTradeable,
  resolveUnderlyingTradingsymbol,
} from '$lib/data/resolveUnderlying.js';
import { seedRootMap } from '$lib/data/rootOf.js';

// ── Fix #14: MCX_COMMODITIES must match backend MCX_VIRTUAL_ROOTS ─────────────

describe('MCX_COMMODITIES — sync with backend MCX_VIRTUAL_ROOTS', () => {
  // Backend MCX_VIRTUAL_ROOTS (from symbol_resolver.py):
  // CRUDEOIL, CRUDEOILM, NATURALGAS, NATGASMINI,
  // GOLD, GOLDM, GOLDGUINEA, GOLDPETAL,
  // SILVER, SILVERM, SILVERMIC,
  // COPPER, ZINC, LEAD, ALUMINIUM, NICKEL,
  // MENTHAOIL, COTTON, CPO

  const EXPECTED_PRESENT = [
    'CRUDEOIL', 'CRUDEOILM', 'NATURALGAS', 'NATGASMINI',
    'GOLD', 'GOLDM', 'GOLDGUINEA', 'GOLDPETAL',
    'SILVER', 'SILVERM', 'SILVERMIC',
    'COPPER', 'ZINC', 'LEAD', 'ALUMINIUM', 'NICKEL',
    'MENTHAOIL', 'COTTON', 'CPO',
  ];

  // These were removed because they are discontinued contracts not present
  // in the backend MCX_VIRTUAL_ROOTS.
  const EXPECTED_ABSENT = [
    'GOLDMINI', 'SILVERMINI', 'ZINCMINI', 'LEADMINI',
    'ALUMINI', 'CASTORSEED', 'KAPAS', 'CARDAMOM',
  ];

  for (const sym of EXPECTED_PRESENT) {
    it(`contains '${sym}'`, () => {
      expect(MCX_COMMODITIES.has(sym)).toBe(true);
    });
  }

  for (const sym of EXPECTED_ABSENT) {
    it(`does NOT contain discontinued '${sym}'`, () => {
      expect(MCX_COMMODITIES.has(sym)).toBe(false);
    });
  }

  it('contains CPO (added to match backend)', () => {
    expect(MCX_COMMODITIES.has('CPO')).toBe(true);
  });
});

// ── Fix #4: CDS_CURRENCIES must match backend CDS_VIRTUAL_ROOTS ──────────────

describe('CDS_CURRENCIES — sync with backend CDS_VIRTUAL_ROOTS', () => {
  // Backend CDS_VIRTUAL_ROOTS: USDINR, EURINR, GBPINR, JPYINR

  it('contains USDINR (original entry)', () => {
    expect(CDS_CURRENCIES.has('USDINR')).toBe(true);
  });

  it('contains EURINR (added)', () => {
    expect(CDS_CURRENCIES.has('EURINR')).toBe(true);
  });

  it('contains GBPINR (added)', () => {
    expect(CDS_CURRENCIES.has('GBPINR')).toBe(true);
  });

  it('contains JPYINR (added)', () => {
    expect(CDS_CURRENCIES.has('JPYINR')).toBe(true);
  });

  it('has exactly 4 entries matching backend', () => {
    expect(CDS_CURRENCIES.size).toBe(4);
  });
});

// ── resolveUnderlying routing for CDS symbols ─────────────────────────────────

describe('resolveUnderlying — CDS currency routing', () => {
  // findNearestFut stub that returns a resolved future for known roots.
  function makeFindFut(rootToFut) {
    return (root) => rootToFut[root] ?? null;
  }

  it('EURINR routes to fut path (not NSE equity) when future available', () => {
    const findFut = makeFindFut({
      EURINR: { s: 'EURINR26JUNFUT', e: 'CDS' },
    });
    const result = resolveUnderlying('EURINR', findFut);
    expect(result).not.toBeNull();
    expect(result.exchange).toBe('CDS');
    expect(result.tradingsymbol).toBe('EURINR26JUNFUT');
    expect(result.kind).toBe('fut');
    expect(result.underlying_group).toBe('EURINR');
  });

  it('GBPINR routes to fut path (not NSE equity) when future available', () => {
    const findFut = makeFindFut({
      GBPINR: { s: 'GBPINR26JUNFUT', e: 'CDS' },
    });
    const result = resolveUnderlying('GBPINR', findFut);
    expect(result).not.toBeNull();
    expect(result.exchange).toBe('CDS');
    expect(result.kind).toBe('fut');
  });

  it('JPYINR routes to fut path (not NSE equity) when future available', () => {
    const findFut = makeFindFut({
      JPYINR: { s: 'JPYINR26JUNFUT', e: 'CDS' },
    });
    const result = resolveUnderlying('JPYINR', findFut);
    expect(result).not.toBeNull();
    expect(result.exchange).toBe('CDS');
    expect(result.kind).toBe('fut');
  });

  it('EURINR returns null when no nearest future is available', () => {
    const result = resolveUnderlying('EURINR', () => null);
    expect(result).toBeNull();
  });

  it('USDINR still resolves correctly (existing behaviour preserved)', () => {
    const findFut = makeFindFut({
      USDINR: { s: 'USDINR26JUNFUT', e: 'CDS' },
    });
    const result = resolveUnderlying('USDINR', findFut);
    expect(result).not.toBeNull();
    expect(result.exchange).toBe('CDS');
    expect(result.tradingsymbol).toBe('USDINR26JUNFUT');
  });
});

// ── KITE_INDEX_QUOTE_KEY_TO_ROOT completeness (Fix #18 reference) ─────────────

describe('KITE_INDEX_QUOTE_KEY_TO_ROOT — SENSEX and BANKEX present', () => {
  it('contains SENSEX → SENSEX', () => {
    expect(KITE_INDEX_QUOTE_KEY_TO_ROOT['SENSEX']).toBe('SENSEX');
  });

  it('contains BANKEX → BANKEX', () => {
    expect(KITE_INDEX_QUOTE_KEY_TO_ROOT['BANKEX']).toBe('BANKEX');
  });

  it('contains all 7 index keys from resolveUnderlying', () => {
    expect(Object.keys(KITE_INDEX_QUOTE_KEY_TO_ROOT)).toHaveLength(7);
  });
});

// ── resolveUnderlying — existing behaviour preserved ─────────────────────────

describe('resolveUnderlying — existing behaviour', () => {
  it('NIFTY routes to NSE spot index', () => {
    const result = resolveUnderlying('NIFTY', null);
    expect(result).not.toBeNull();
    expect(result.exchange).toBe('NSE');
    expect(result.tradingsymbol).toBe('NIFTY 50');
    expect(result.kind).toBe('spot');
  });

  it('CRUDEOIL routes to MCX future when resolver returns one', () => {
    const findFut = (root) => (root === 'CRUDEOIL' ? { s: 'CRUDEOIL26JUNFUT', e: 'MCX' } : null);
    const result = resolveUnderlying('CRUDEOIL', findFut);
    expect(result).not.toBeNull();
    expect(result.exchange).toBe('MCX');
    expect(result.kind).toBe('fut');
  });

  it('unknown equity routes to NSE spot', () => {
    const result = resolveUnderlying('RELIANCE', null);
    expect(result).not.toBeNull();
    expect(result.exchange).toBe('NSE');
    expect(result.tradingsymbol).toBe('RELIANCE');
    expect(result.kind).toBe('spot');
  });

  it('lowercase input is normalised', () => {
    const result = resolveUnderlying('nifty', null);
    expect(result).not.toBeNull();
    expect(result.underlying_group).toBe('NIFTY');
  });

  it('empty string returns null', () => {
    expect(resolveUnderlying('', null)).toBeNull();
  });
});

// ── resolveAnchorToTradeable — CDS currencies resolve via fut ─────────────────

describe('resolveAnchorToTradeable — CDS anchor resolution', () => {
  it('EURINR resolves to nearest future when instruments are warm', () => {
    const findFut = (root) => (root === 'EURINR' ? { s: 'EURINR26JUNFUT', e: 'CDS' } : null);
    const result = resolveAnchorToTradeable('EURINR', findFut);
    expect(result).toBe('EURINR26JUNFUT');
  });

  it('JPYINR resolves to nearest future', () => {
    const findFut = (root) => (root === 'JPYINR' ? { s: 'JPYINR26JUNFUT', e: 'CDS' } : null);
    const result = resolveAnchorToTradeable('JPYINR', findFut);
    expect(result).toBe('JPYINR26JUNFUT');
  });
});

// ── Bug C: _NEXT virtual roots in resolveUnderlying ──────────────────────────

describe('resolveUnderlying — _NEXT virtual roots', () => {
  it('CRUDEOIL_NEXT → MCX exchange, underlying_group=CRUDEOIL, kind=fut', () => {
    const findFut = (root) => (root === 'CRUDEOIL' ? { s: 'CRUDEOILM26SEPFUT', e: 'MCX' } : null);
    const result = resolveUnderlying('CRUDEOIL_NEXT', findFut);
    expect(result).not.toBeNull();
    expect(result.exchange).toBe('MCX');
    expect(result.underlying_group).toBe('CRUDEOIL');
    expect(result.kind).toBe('fut');
    expect(result.tradingsymbol).toBe('CRUDEOILM26SEPFUT');
  });

  it('USDINR_NEXT → CDS exchange, underlying_group=USDINR, kind=fut', () => {
    const findFut = (root) => (root === 'USDINR' ? { s: 'USDINR26SEPFUT', e: 'CDS' } : null);
    const result = resolveUnderlying('USDINR_NEXT', findFut);
    expect(result).not.toBeNull();
    expect(result.exchange).toBe('CDS');
    expect(result.underlying_group).toBe('USDINR');
    expect(result.kind).toBe('fut');
  });

  it('EURINR_NEXT → CDS exchange, underlying_group=EURINR', () => {
    const findFut = (root) => (root === 'EURINR' ? { s: 'EURINR26SEPFUT', e: 'CDS' } : null);
    const result = resolveUnderlying('EURINR_NEXT', findFut);
    expect(result).not.toBeNull();
    expect(result.exchange).toBe('CDS');
    expect(result.underlying_group).toBe('EURINR');
  });

  it('RELIANCE (bare) → NSE spot, tradingsymbol=RELIANCE, underlying_group=RELIANCE', () => {
    const result = resolveUnderlying('RELIANCE', null);
    expect(result).not.toBeNull();
    expect(result.exchange).toBe('NSE');
    expect(result.tradingsymbol).toBe('RELIANCE');
    expect(result.underlying_group).toBe('RELIANCE');
  });

  it('NIFTY26JUNFUT passes through to NSE with tradingsymbol preserved', () => {
    const result = resolveUnderlying('NIFTY26JUNFUT', null);
    expect(result).not.toBeNull();
    expect(result.exchange).toBe('NSE');
    expect(result.tradingsymbol).toBe('NIFTY26JUNFUT');
  });

  it('CRUDEOIL (bare root, no _NEXT) still routes to MCX front-month', () => {
    const findFut = (root) => (root === 'CRUDEOIL' ? { s: 'CRUDEOIL26JUNFUT', e: 'MCX' } : null);
    const result = resolveUnderlying('CRUDEOIL', findFut);
    expect(result).not.toBeNull();
    expect(result.exchange).toBe('MCX');
    expect(result.underlying_group).toBe('CRUDEOIL');
    expect(result.kind).toBe('fut');
  });
});

// ── §5: _NEXT means back/next-month, not front-month ─────────────────────────
//
// rootOf.js / ChartWorkspace.svelte establish `_NEXT` as meaning the
// BACK-month contract (e.g. CRUDEOIL_NEXT → CRUDEOIL26JULFUT when
// front-month is CRUDEOIL26JUNFUT) — resolveUnderlying() must resolve it
// the same way (via rootOf.js's seeded two-slot map), not silently
// collapse it to front-month via findNearestFut. These tests seed
// rootOf.js's map directly (seedRootMap) so the back-month slot actually
// differs from the findNearestFut front-month stub, proving the two
// resolutions diverge — the earlier "_NEXT virtual roots" describe block
// above doesn't seed the map, so it only exercises the cold-cache
// fallback path (front-month), not this back-month-resolved path.

describe('resolveUnderlying — _NEXT resolves to BACK-month, not front-month (§5)', () => {
  afterEach(() => {
    seedRootMap({}, {}); // reset module-level state between tests
  });

  it('CRUDEOIL_NEXT resolves to the back-month slot, diverging from findNearestFut front-month', () => {
    seedRootMap({ CRUDEOIL: ['CRUDEOIL26JUNFUT', 'CRUDEOIL26JULFUT'] }, {});
    // findNearestFut stub only knows the front-month contract — if
    // resolveUnderlying fell back to it for _NEXT, the test would see
    // CRUDEOIL26JUNFUT (front) instead of CRUDEOIL26JULFUT (back).
    const findFut = (root) => (root === 'CRUDEOIL' ? { s: 'CRUDEOIL26JUNFUT', e: 'MCX' } : null);
    const result = resolveUnderlying('CRUDEOIL_NEXT', findFut);
    expect(result).not.toBeNull();
    expect(result.tradingsymbol).toBe('CRUDEOIL26JULFUT');
    expect(result.tradingsymbol).not.toBe('CRUDEOIL26JUNFUT');
    expect(result.exchange).toBe('MCX');
    expect(result.kind).toBe('fut');
    expect(result.underlying_group).toBe('CRUDEOIL');
    expect(result.quoteKey).toBe('MCX:CRUDEOIL26JULFUT');
  });

  it('CRUDEOIL (no _NEXT) still resolves to front-month even when the back-month map is seeded', () => {
    seedRootMap({ CRUDEOIL: ['CRUDEOIL26JUNFUT', 'CRUDEOIL26JULFUT'] }, {});
    const findFut = (root) => (root === 'CRUDEOIL' ? { s: 'CRUDEOIL26JUNFUT', e: 'MCX' } : null);
    const result = resolveUnderlying('CRUDEOIL', findFut);
    expect(result.tradingsymbol).toBe('CRUDEOIL26JUNFUT');
  });

  it('USDINR_NEXT resolves to the back-month CDS slot', () => {
    seedRootMap({}, { USDINR: ['USDINR26JUNFUT', 'USDINR26JULFUT'] });
    const findFut = (root) => (root === 'USDINR' ? { s: 'USDINR26JUNFUT', e: 'CDS' } : null);
    const result = resolveUnderlying('USDINR_NEXT', findFut);
    expect(result.tradingsymbol).toBe('USDINR26JULFUT');
    expect(result.exchange).toBe('CDS');
  });

  it('back-month slot absent (front-only map): falls through to findNearestFut front-month rather than returning nothing', () => {
    seedRootMap({ CRUDEOIL: ['CRUDEOIL26JUNFUT'] }, {}); // no back-month slot
    const findFut = (root) => (root === 'CRUDEOIL' ? { s: 'CRUDEOIL26JUNFUT', e: 'MCX' } : null);
    const result = resolveUnderlying('CRUDEOIL_NEXT', findFut);
    expect(result).not.toBeNull();
    expect(result.tradingsymbol).toBe('CRUDEOIL26JUNFUT');
  });

  it('cold map (not seeded at all): falls through to findNearestFut front-month', () => {
    const findFut = (root) => (root === 'CRUDEOIL' ? { s: 'CRUDEOIL26JUNFUT', e: 'MCX' } : null);
    const result = resolveUnderlying('CRUDEOIL_NEXT', findFut);
    expect(result).not.toBeNull();
    expect(result.tradingsymbol).toBe('CRUDEOIL26JUNFUT');
  });
});

// ── Bug C: _NEXT virtual roots in resolveAnchorToTradeable ───────────────────

describe('resolveAnchorToTradeable — _NEXT virtual roots', () => {
  it('EURINR_NEXT resolves to the back-month contract via findNearestFut', () => {
    // The back-month resolver returns the nearest future for the bare root;
    // resolveAnchorToTradeable strips _NEXT before the lookup.
    const findFut = (root) => (root === 'EURINR' ? { s: 'EURINR26SEPFUT', e: 'CDS' } : null);
    const result = resolveAnchorToTradeable('EURINR_NEXT', findFut);
    expect(result).toBe('EURINR26SEPFUT');
  });

  it('CRUDEOIL_NEXT resolves to the nearest future for CRUDEOIL root', () => {
    const findFut = (root) => (root === 'CRUDEOIL' ? { s: 'CRUDEOILM26SEPFUT', e: 'MCX' } : null);
    const result = resolveAnchorToTradeable('CRUDEOIL_NEXT', findFut);
    expect(result).toBe('CRUDEOILM26SEPFUT');
  });
});

// ── resolveUnderlyingTradingsymbol — shared front-month resolution boundary ──
// Used by underlyingSpotStore.svelte.js's getUnderlyingSpot() (NavStrip) and
// the derivatives page's Snapshot/_undLive + liveSpot resolution — the fix
// that unifies all three surfaces onto the same front-month contract
// (operator-confirmed Option B: always front-month, everywhere).

describe('resolveUnderlyingTradingsymbol — front-month resolution', () => {
  it('NIFTY resolves to its NSE spot tradingsymbol', () => {
    expect(resolveUnderlyingTradingsymbol('NIFTY', null)).toBe('NIFTY 50');
  });

  it('CRUDEOIL resolves to the front-month future when the resolver is warm', () => {
    const findFut = (root) => (root === 'CRUDEOIL' ? { s: 'CRUDEOIL26JUNFUT', e: 'MCX' } : null);
    expect(resolveUnderlyingTradingsymbol('CRUDEOIL', findFut)).toBe('CRUDEOIL26JUNFUT');
  });

  it('CRUDEOIL_NEXT strips the _NEXT suffix and calls findNearestFut with the bare root, returning front-month', () => {
    let calledWith = null;
    const findFut = (root) => {
      calledWith = root;
      return root === 'CRUDEOIL' ? { s: 'CRUDEOILM26SEPFUT', e: 'MCX' } : null;
    };
    const result = resolveUnderlyingTradingsymbol('CRUDEOIL_NEXT', findFut);
    expect(calledWith).toBe('CRUDEOIL');
    expect(result).toBe('CRUDEOILM26SEPFUT'); // front-month, not a far-month/anchor contract
  });

  it('cold instruments cache: MCX root with no resolvable future falls back to the bare root (never null/undefined)', () => {
    const result = resolveUnderlyingTradingsymbol('GOLDM', () => null);
    expect(result).toBe('GOLDM');
  });

  it('cold instruments cache: CDS root with no resolvable future falls back to the bare root (never null/undefined)', () => {
    // resolveUnderlying() returns null for CDS with no future — the wrapper
    // must still hand back a usable string, not null, so callers (e.g.
    // liveSnap(ts)) never get a broken lookup key.
    const result = resolveUnderlyingTradingsymbol('USDINR', () => null);
    expect(result).toBe('USDINR');
  });

  it('unknown equity passes through unchanged (already tradeable)', () => {
    expect(resolveUnderlyingTradingsymbol('RELIANCE', null)).toBe('RELIANCE');
  });

  it('lowercase input is normalised to uppercase', () => {
    expect(resolveUnderlyingTradingsymbol('reliance', null)).toBe('RELIANCE');
  });

  it('empty string falls back to empty string, not null/undefined', () => {
    expect(resolveUnderlyingTradingsymbol('', null)).toBe('');
  });
});
