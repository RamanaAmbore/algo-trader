import { describe, it, expect } from 'vitest';
import {
  priceFmt,
  aggCompact,
  qtyFmt,
  directional,
  fmtPctScaled,
  fmtPctFraction,
} from '$lib/format.js';

// ── priceFmt ────────────────────────────────────────────────────────────────

describe('priceFmt', () => {
  it('formats small prices with 2 decimals', () => {
    expect(priceFmt(552.30)).toBe('552.30');
  });

  it('formats large prices with 2 decimals (always 2dp)', () => {
    expect(priceFmt(22000)).toBe('22,000.00');
  });

  it('formats negative prices', () => {
    expect(priceFmt(-50.5)).toBe('-50.50');
  });

  it('formats zero', () => {
    expect(priceFmt(0)).toBe('0.00');
  });

  it('returns em-dash for null', () => {
    expect(priceFmt(null)).toBe('—');
  });

  it('returns em-dash for undefined', () => {
    expect(priceFmt(undefined)).toBe('—');
  });

  it('returns em-dash for NaN', () => {
    expect(priceFmt(NaN)).toBe('—');
  });

  it('returns em-dash for Infinity', () => {
    expect(priceFmt(Infinity)).toBe('—');
  });

  it('returns em-dash for -Infinity', () => {
    expect(priceFmt(-Infinity)).toBe('—');
  });
});

// ── aggCompact ──────────────────────────────────────────────────────────────

describe('aggCompact', () => {
  // < 1K — applies decimal rule (|v| < 100 → 2dp, |v| ≥ 100 → 0dp)
  it('< 100 uses decimal rule (2dp)', () => {
    expect(aggCompact(50)).toBe('50.00');
  });

  it('≥ 100 and < 1K uses decimal rule (0dp, no suffix)', () => {
    expect(aggCompact(500)).toBe('500');
  });

  it('< 100, negative', () => {
    expect(aggCompact(-50)).toBe('-50.00');
  });

  it('≥ 100 and < 1K, negative', () => {
    expect(aggCompact(-500)).toBe('-500');
  });

  // 1K–1L range → "K" suffix (rounded, no decimal)
  it('1000 → "1K"', () => {
    expect(aggCompact(1000)).toBe('1K');
  });

  it('1500 → "2K" (rounded)', () => {
    expect(aggCompact(1500)).toBe('2K');
  });

  it('negative 1000 → "-1K"', () => {
    expect(aggCompact(-1000)).toBe('-1K');
  });

  // 1L–1Cr range → "X.XXL"
  it('100000 → "1.00L"', () => {
    expect(aggCompact(100000)).toBe('1.00L');
  });

  it('150000 → "1.50L"', () => {
    expect(aggCompact(150000)).toBe('1.50L');
  });

  it('negative lakh', () => {
    expect(aggCompact(-100000)).toBe('-1.00L');
  });

  // ≥ 1Cr → "X.XXC"
  it('10000000 → "1.00C"', () => {
    expect(aggCompact(10000000)).toBe('1.00C');
  });

  it('25000000 → "2.50C"', () => {
    expect(aggCompact(25000000)).toBe('2.50C');
  });

  it('negative crore', () => {
    expect(aggCompact(-10000000)).toBe('-1.00C');
  });

  // Edge cases
  it('zero → "0.00"', () => {
    expect(aggCompact(0)).toBe('0.00');
  });

  it('null → "—"', () => {
    expect(aggCompact(null)).toBe('—');
  });

  it('NaN → "—"', () => {
    expect(aggCompact(NaN)).toBe('—');
  });

  it('Infinity → "—"', () => {
    expect(aggCompact(Infinity)).toBe('—');
  });
});

// ── qtyFmt ──────────────────────────────────────────────────────────────────

describe('qtyFmt', () => {
  it('formats integer qty', () => {
    expect(qtyFmt(100)).toBe('100');
  });

  it('rounds fractional quantities', () => {
    expect(qtyFmt(100.7)).toBe('101');
  });

  it('formats large qty with Indian grouping', () => {
    expect(qtyFmt(150432)).toBe('1,50,432');
  });

  it('zero', () => {
    expect(qtyFmt(0)).toBe('0');
  });

  it('negative qty', () => {
    expect(qtyFmt(-50)).toBe('-50');
  });

  it('null → "—"', () => {
    expect(qtyFmt(null)).toBe('—');
  });

  it('NaN → "—"', () => {
    expect(qtyFmt(NaN)).toBe('—');
  });
});

// ── directional ─────────────────────────────────────────────────────────────

describe('directional', () => {
  it('long position passes through positive value', () => {
    expect(directional(1.5, 10)).toBe(1.5);
  });

  it('no position (netQty=0) passes through value', () => {
    expect(directional(1.5, 0)).toBe(1.5);
  });

  it('short position negates positive value', () => {
    expect(directional(1.5, -5)).toBe(-1.5);
  });

  it('short position negates a negative value (double-negate → positive)', () => {
    expect(directional(-2.0, -5)).toBe(2.0);
  });

  it('null value returns null', () => {
    expect(directional(null, 10)).toBeNull();
  });

  it('undefined value returns undefined', () => {
    expect(directional(undefined, 10)).toBeUndefined();
  });

  it('NaN value is returned as-is (non-finite pass-through)', () => {
    expect(directional(NaN, 10)).toBeNaN();
  });

  it('zero value, short position → -0 (IEEE 754 negation of 0)', () => {
    // -Number(0) is -0 in IEEE 754; Object.is distinguishes -0 from +0
    const result = directional(0, -5);
    expect(Object.is(result, -0)).toBe(true);
  });
});

// ── fmtPctScaled ────────────────────────────────────────────────────────────

describe('fmtPctScaled', () => {
  it('5.0 → "5.00%"', () => {
    expect(fmtPctScaled(5.0)).toBe('5.00%');
  });

  it('precision override: 5.0, decimals=1 → "5.0%"', () => {
    expect(fmtPctScaled(5.0, 1)).toBe('5.0%');
  });

  it('signed=true, positive: +5.0%', () => {
    expect(fmtPctScaled(5.0, 1, true)).toBe('+5.0%');
  });

  it('signed=true, zero: +0.0%', () => {
    expect(fmtPctScaled(0, 1, true)).toBe('+0.0%');
  });

  it('signed=true, negative: no double sign', () => {
    expect(fmtPctScaled(-3.5, 1, true)).toBe('-3.5%');
  });

  it('large value uses 0dp via decimal rule', () => {
    expect(fmtPctScaled(120)).toBe('120%');
  });

  it('null → "—"', () => {
    expect(fmtPctScaled(null)).toBe('—');
  });

  it('NaN → "—"', () => {
    expect(fmtPctScaled(NaN)).toBe('—');
  });
});

// ── fmtPctFraction ──────────────────────────────────────────────────────────

describe('fmtPctFraction', () => {
  it('0.05 → "5.00%"', () => {
    expect(fmtPctFraction(0.05)).toBe('5.00%');
  });

  it('0 → "0.00%"', () => {
    expect(fmtPctFraction(0)).toBe('0.00%');
  });

  it('precision override: 0.05, decimals=1 → "5.0%"', () => {
    expect(fmtPctFraction(0.05, 1)).toBe('5.0%');
  });

  it('signed=true, positive fraction', () => {
    expect(fmtPctFraction(0.05, 1, true)).toBe('+5.0%');
  });

  it('negative fraction', () => {
    expect(fmtPctFraction(-0.03, 1)).toBe('-3.0%');
  });

  it('null → "—"', () => {
    expect(fmtPctFraction(null)).toBe('—');
  });
});
