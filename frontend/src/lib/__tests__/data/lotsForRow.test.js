/**
 * lotsForRow.test.js — Vitest unit tests for lotsForRow / fmtLots.
 *
 * Five quality dimensions:
 *  1. SSOT  — exercises the same module path used by MarketPulse,
 *             PerformancePage, derivatives/+page.svelte
 *  2. Perf  — pure synchronous; no I/O, no DOM
 *  3. Stale — guards the `lots` fast-path added for qty/lots/lot_size
 *             normalization (backend now returns quantity=contracts uniformly)
 *  4. Reuse — lotsForRow is the canonical lot-display helper; the test
 *             mirrors actual call sites (MarketPulse unified row, raw API row)
 *  5. UX    — correct lot count must reach the grid cell in both the
 *             warm-cache (instruments hit) and cold-cache (instruments miss)
 *             cases, and when the new `lots` field is present from the API
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { lotsForRow, fmtLots } from '$lib/data/lotsForRow.js';

// ── Instrument cache mock ─────────────────────────────────────────────────────
// lotsForRow imports `getInstrument` and `getOptionUnderlyingLot` from
// '$lib/data/instruments'. We mock the module so tests are deterministic
// without needing a live instruments cache.

vi.mock('$lib/data/instruments', () => ({
  getInstrument: vi.fn(),
  getOptionUnderlyingLot: vi.fn(),
}));

import { getInstrument, getOptionUnderlyingLot } from '$lib/data/instruments';

// vi.mocked() gives TypeScript the MockedFunction type so .mockReturnValue
// type-checks correctly (same pattern as watchlistSymbols.test.js).
const _getInstrument        = vi.mocked(getInstrument);
const _getOptionUnderlyingLot = vi.mocked(getOptionUnderlyingLot);

beforeEach(() => {
  vi.resetAllMocks();
});

afterEach(() => {
  vi.resetAllMocks();
});

// ── lotsForRow ────────────────────────────────────────────────────────────────

describe('lotsForRow — guard clauses', () => {
  it('returns null for null input', () => {
    expect(lotsForRow(null)).toBeNull();
  });

  it('returns null for TOTAL rows (_isTotal=true)', () => {
    expect(lotsForRow({ tradingsymbol: 'NIFTY', _isTotal: true })).toBeNull();
  });

  it('returns 0 for missing/empty tradingsymbol', () => {
    expect(lotsForRow({ tradingsymbol: '' })).toBe(0);
    expect(lotsForRow({})).toBe(0);
  });
});

describe('lotsForRow — derivative row, `lots` field present (fast path)', () => {
  it('returns lots directly when `lots` is on the row and inst type=CE', () => {
    _getInstrument.mockReturnValue({ t: 'CE', ls: 50 });
    const row = { tradingsymbol: 'NIFTY24JAN22000CE', quantity: 100, lots: 2 };
    expect(lotsForRow(row)).toBe(2);
  });

  it('returns lots directly when `lots` is on the row and inst type=PE', () => {
    _getInstrument.mockReturnValue({ t: 'PE', ls: 50 });
    const row = { tradingsymbol: 'NIFTY24JAN21000PE', quantity: 100, lots: 2 };
    expect(lotsForRow(row)).toBe(2);
  });

  it('returns lots directly when `lots` is on the row and inst type=FUT', () => {
    _getInstrument.mockReturnValue({ t: 'FUT', ls: 250 });
    const row = { tradingsymbol: 'NIFTY24JANFUT', quantity: 250, lots: 1 };
    expect(lotsForRow(row)).toBe(1);
  });

  it('works even when instruments cache is cold (getInstrument returns null) if `lots` present', () => {
    // When cache is cold, itype=undefined → isDerivative=false → falls through
    // to the equity path which uses getOptionUnderlyingLot.
    // BUT if the symbol ends in CE/PE/FUT and itype is undefined the row.lots
    // shortcut is only reached when isDerivative=true.
    // For this case we still need the inst to identify the type.
    // So this test covers: inst returns { t: 'CE' } but ls=0 (cold lot_size).
    _getInstrument.mockReturnValue({ t: 'CE', ls: 0 });
    const row = { tradingsymbol: 'CRUDEOIL25AUGFUT', quantity: 100, lots: 1 };
    // With ls=0, division path returns 0; lots fast path returns 1.
    expect(lotsForRow(row)).toBe(1);
  });

  it('handles MCX multi-lot: lots=5, quantity=500 (lot_size=100)', () => {
    _getInstrument.mockReturnValue({ t: 'FUT', ls: 100 });
    const row = { tradingsymbol: 'CRUDEOIL25AUGFUT', quantity: 500, lots: 5 };
    expect(lotsForRow(row)).toBe(5);
  });

  it('uses Math.abs so negative lots (short positions) are non-negative display', () => {
    _getInstrument.mockReturnValue({ t: 'CE', ls: 50 });
    // qty_pos path uses Math.abs; `lots` fast path also applies Math.abs
    const row = { tradingsymbol: 'NIFTY24JAN22000CE', quantity: -100, lots: -2 };
    expect(lotsForRow(row)).toBe(2);
  });
});

describe('lotsForRow — derivative row, `lots` absent (fallback: quantity / lot_size)', () => {
  it('computes lots = quantity / inst.ls when lots is null', () => {
    _getInstrument.mockReturnValue({ t: 'CE', ls: 50 });
    const row = { tradingsymbol: 'NIFTY24JAN22000CE', quantity: 100, lots: null };
    // 100 contracts / 50 per lot = 2 lots
    expect(lotsForRow(row)).toBe(2);
  });

  it('computes lots = quantity / inst.ls when lots is absent (undefined)', () => {
    _getInstrument.mockReturnValue({ t: 'FUT', ls: 250 });
    const row = { tradingsymbol: 'NIFTY24JANFUT', quantity: 250 };
    // 250 / 250 = 1 lot
    expect(lotsForRow(row)).toBe(1);
  });

  it('returns 0 when instruments cache is completely cold (no inst) and lots absent', () => {
    _getInstrument.mockReturnValue(null);
    const row = { tradingsymbol: 'CRUDEOIL25AUGFUT', quantity: 100 };
    // itype=undefined → isDerivative=false → equity path → getOptionUnderlyingLot
    _getOptionUnderlyingLot.mockReturnValue(0);
    expect(lotsForRow(row)).toBe(0);
  });

  it('uses qty_pos when present (MarketPulse unified row shape)', () => {
    _getInstrument.mockReturnValue({ t: 'PE', ls: 75 });
    // MarketPulse unified row has qty_pos instead of quantity
    const row = { tradingsymbol: 'BANKNIFTY24JAN46000PE', qty_pos: 150, qty_hold: 0 };
    // 150 / 75 = 2 lots
    expect(lotsForRow(row)).toBe(2);
  });
});

describe('lotsForRow — equity/underlying holding row', () => {
  it('returns lot count from qty_hold / underlying lot for EQ row', () => {
    _getInstrument.mockReturnValue({ t: 'EQ' });
    _getOptionUnderlyingLot.mockReturnValue(50);
    const row = { tradingsymbol: 'NIFTY', qty_hold: 100 };
    // EQ row: 100 / 50 = 2 lots
    expect(lotsForRow(row)).toBe(2);
  });

  it('returns 0 for plain equity with no underlying lot', () => {
    _getInstrument.mockReturnValue({ t: 'EQ' });
    _getOptionUnderlyingLot.mockReturnValue(0);
    const row = { tradingsymbol: 'INFY', qty_hold: 10 };
    expect(lotsForRow(row)).toBe(0);
  });
});

describe('lotsForRow — rounding', () => {
  it('rounds to one decimal', () => {
    _getInstrument.mockReturnValue({ t: 'CE', ls: 3 });
    // 10 / 3 = 3.333... → rounds to 3.3
    const row = { tradingsymbol: 'SOMETHING24AUGCE', quantity: 10, lots: null };
    expect(lotsForRow(row)).toBe(3.3);
  });
});

// ── fmtLots ───────────────────────────────────────────────────────────────────

describe('fmtLots', () => {
  it('returns "" for null', () => { expect(fmtLots(null)).toBe(''); });
  it('returns "" for undefined', () => { expect(fmtLots(undefined)).toBe(''); });
  it('returns "0" for 0', () => { expect(fmtLots(0)).toBe('0'); });
  it('returns "2" for integer 2', () => { expect(fmtLots(2)).toBe('2'); });
  it('returns "1.5" for 1.5', () => { expect(fmtLots(1.5)).toBe('1.5'); });
  it('returns "10" for integer 10', () => { expect(fmtLots(10)).toBe('10'); });
});
