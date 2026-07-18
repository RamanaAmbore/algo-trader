import { describe, it, expect, beforeEach } from 'vitest';
import {
  rootOf,
  rootOfLabel,
  resolveVirtual,
  seedRootMap,
  seedRootMapFromInstruments,
} from '$lib/data/rootOf.js';

// ── Test fixture helpers ─────────────────────────────────────────────────────

/**
 * Instrument fixture using short-form field names matching the Instrument struct:
 *   s = tradingsymbol
 *   e = exchange
 *   t = instrument_type
 *   u = name / underlying root
 *   x = expiry (YYYY-MM-DD)
 */
function makeInstrument(overrides = {}) {
  return {
    s: 'CRUDEOIL26JUNFUT',
    e: 'MCX',
    t: 'FUT',
    u: 'CRUDEOIL',
    x: '2026-06-19',
    ...overrides,
  };
}

// Seed a known deterministic map before each test
const FRONT = 'CRUDEOIL26JUNFUT';
const BACK  = 'CRUDEOIL26JULFUT';
const FAR   = 'CRUDEOIL26AUGFUT';

beforeEach(() => {
  // Reset to a known state: CRUDEOIL has front + back on MCX; CDS empty
  seedRootMap({ CRUDEOIL: [FRONT, BACK] }, {});
});

// ── rootOf ───────────────────────────────────────────────────────────────────

describe('rootOf', () => {
  it('front-month contract → bare root', () => {
    expect(rootOf(FRONT, 'MCX')).toBe('CRUDEOIL');
  });

  it('back-month contract → "ROOT_NEXT"', () => {
    expect(rootOf(BACK, 'MCX')).toBe('CRUDEOIL_NEXT');
  });

  it('far-month contract (not in map) → pass-through raw symbol', () => {
    expect(rootOf(FAR, 'MCX')).toBe(FAR);
  });

  it('non-FUT instrument (option symbol) → pass-through (MCX)', () => {
    // Options contain digits and letters but don't match FUT_RE
    expect(rootOf('CRUDEOIL2610050CE', 'MCX')).toBe('CRUDEOIL2610050CE');
  });

  it('equity symbol on non-MCX/CDS exchange → pass-through', () => {
    expect(rootOf('RELIANCE', 'NSE')).toBe('RELIANCE');
  });

  it('unknown exchange → pass-through', () => {
    expect(rootOf('CRUDEOIL26JUNFUT', 'NFO')).toBe('CRUDEOIL26JUNFUT');
  });

  it('empty string → empty string', () => {
    expect(rootOf('', 'MCX')).toBe('');
  });

  it('case-insensitive contract matching', () => {
    expect(rootOf(FRONT.toLowerCase(), 'MCX')).toBe('CRUDEOIL');
  });
});

// ── resolveVirtual ───────────────────────────────────────────────────────────

describe('resolveVirtual', () => {
  it('"CRUDEOIL" → front-month tradingsymbol', () => {
    expect(resolveVirtual('CRUDEOIL', 'MCX')).toBe(FRONT);
  });

  it('"CRUDEOIL_NEXT" → back-month tradingsymbol', () => {
    expect(resolveVirtual('CRUDEOIL_NEXT', 'MCX')).toBe(BACK);
  });

  it('unknown virtual (all-alpha, not in map) → returns input unchanged', () => {
    expect(resolveVirtual('UNKNOWN', 'MCX')).toBe('UNKNOWN');
  });

  it('non-virtual symbol (has digits) → pass-through', () => {
    expect(resolveVirtual('CRUDEOIL26JUNFUT', 'MCX')).toBe('CRUDEOIL26JUNFUT');
  });

  it('non-MCX/CDS exchange → pass-through', () => {
    expect(resolveVirtual('GOLD', 'NFO')).toBe('GOLD');
  });

  it('empty string → empty string', () => {
    expect(resolveVirtual('', 'MCX')).toBe('');
  });
});

// ── rootOfLabel ──────────────────────────────────────────────────────────────

describe('rootOfLabel', () => {
  it('front-month → bare root label, no suffix', () => {
    expect(rootOfLabel(FRONT, 'MCX')).toBe('CRUDEOIL');
  });

  it('"_NEXT" internal suffix → ".NEXT" display suffix', () => {
    expect(rootOfLabel(BACK, 'MCX')).toBe('CRUDEOIL.NEXT');
  });

  it('far-month → raw symbol as label', () => {
    expect(rootOfLabel(FAR, 'MCX')).toBe(FAR);
  });
});

// ── seedRootMapFromInstruments ────────────────────────────────────────────────

describe('seedRootMapFromInstruments', () => {
  /**
   * Build a set of instruments with controllable expiry dates.
   * We use a far-future year so tests don't expire.
   */
  const futureExpiry1 = '2099-06-19';
  const futureExpiry2 = '2099-07-19';
  const futureExpiry3 = '2099-08-19';

  // Kite FUT format: ROOT + YY + MON + FUT  (e.g. GOLD99JUNFUT)
  const instruments = [
    makeInstrument({ s: 'GOLD99JUNFUT', u: 'GOLD', e: 'MCX', t: 'FUT', x: futureExpiry1 }),
    makeInstrument({ s: 'GOLD99JULFUT', u: 'GOLD', e: 'MCX', t: 'FUT', x: futureExpiry2 }),
    makeInstrument({ s: 'GOLD99AUGFUT', u: 'GOLD', e: 'MCX', t: 'FUT', x: futureExpiry3 }),
    // CDS instrument
    makeInstrument({ s: 'USDINR99JUNFUT',  u: 'USDINR', e: 'CDS', t: 'FUT', x: futureExpiry1 }),
    // Non-FUT (option) — should be skipped
    makeInstrument({ s: 'CRUDEOIL2699CE',  u: 'CRUDEOIL', e: 'MCX', t: 'CE',  x: futureExpiry1 }),
    // No underlying — should be skipped
    makeInstrument({ s: 'UNKNOWNFUT',      u: '',        e: 'MCX', t: 'FUT', x: futureExpiry1 }),
  ];

  beforeEach(() => {
    seedRootMapFromInstruments(instruments);
  });

  it('filters by FUT type and seeded MCX front slot', () => {
    // GOLD front month is the one with earliest expiry
    expect(rootOf('GOLD99JUNFUT', 'MCX')).toBe('GOLD');
  });

  it('back-month slot is second by expiry', () => {
    expect(rootOf('GOLD99JULFUT', 'MCX')).toBe('GOLD_NEXT');
  });

  it('keeps at most 2 slots — third expiry is pass-through', () => {
    expect(rootOf('GOLD99AUGFUT', 'MCX')).toBe('GOLD99AUGFUT');
  });

  it('CDS futures also seeded', () => {
    expect(resolveVirtual('USDINR', 'CDS')).toBe('USDINR99JUNFUT');
  });

  it('CE options skipped (not FUT)', () => {
    // CRUDEOIL has no FUT in this set (only CE), so nothing seeded
    expect(rootOf('CRUDEOIL2699CE', 'MCX')).toBe('CRUDEOIL2699CE');
  });

  it('skips contracts with missing underlying', () => {
    // No crash — just no entry for empty root
    expect(() => rootOf('UNKNOWNFUT', 'MCX')).not.toThrow();
  });

  it('skips settling-today contracts (expiry <= today)', () => {
    // Today is 2026-07-18 per env; past expiry contract should not appear in map
    const pastItems = [
      makeInstrument({ s: 'SILVERM26JUNFUT', u: 'SILVERM', e: 'MCX', t: 'FUT', x: '2026-06-01' }),
    ];
    seedRootMapFromInstruments(pastItems);
    // Should pass through (no slot for SILVERM)
    expect(rootOf('SILVERM26JUNFUT', 'MCX')).toBe('SILVERM26JUNFUT');
  });

  it('non-array input does not throw', () => {
    expect(() => seedRootMapFromInstruments(null)).not.toThrow();
    expect(() => seedRootMapFromInstruments(undefined)).not.toThrow();
  });
});
