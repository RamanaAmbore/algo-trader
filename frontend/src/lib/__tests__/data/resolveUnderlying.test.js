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

import { describe, it, expect } from 'vitest';
import {
  MCX_COMMODITIES,
  CDS_CURRENCIES,
  KITE_INDEX_QUOTE_KEY_TO_ROOT,
  INDEX_LTP_KEY,
  resolveUnderlying,
  resolveAnchorToTradeable,
} from '$lib/data/resolveUnderlying.js';

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
