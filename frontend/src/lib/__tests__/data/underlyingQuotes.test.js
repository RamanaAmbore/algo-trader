import { describe, it, expect } from 'vitest';
import { applyUnderlyingTickLtp } from '$lib/data/underlyingQuoteUtils.js';

const BASE_QUOTES = {
  NIFTY: { ltp: 24000, day_pct: 0.5, prev_close: 23880 },
  BANKNIFTY: { ltp: 52000, day_pct: -0.3, prev_close: 52156 },
};

describe('applyUnderlyingTickLtp', () => {
  it('updates ltp when root is in quotes and ltp is a positive finite number', () => {
    const result = applyUnderlyingTickLtp(BASE_QUOTES, 'NIFTY', 24100);
    expect(result.NIFTY.ltp).toBe(24100);
  });

  it('returns the same object reference when root is not in quotes', () => {
    const result = applyUnderlyingTickLtp(BASE_QUOTES, 'MIDCAP', 10000);
    expect(result).toBe(BASE_QUOTES);
  });

  it('returns the same object reference when ltp is null', () => {
    const result = applyUnderlyingTickLtp(BASE_QUOTES, 'NIFTY', null);
    expect(result).toBe(BASE_QUOTES);
  });

  it('returns the same object reference when ltp is NaN', () => {
    const result = applyUnderlyingTickLtp(BASE_QUOTES, 'NIFTY', NaN);
    expect(result).toBe(BASE_QUOTES);
  });

  it('returns the same object reference when ltp is 0 (non-positive)', () => {
    const result = applyUnderlyingTickLtp(BASE_QUOTES, 'NIFTY', 0);
    expect(result).toBe(BASE_QUOTES);
  });

  it('returns the same object reference when ltp is negative', () => {
    const result = applyUnderlyingTickLtp(BASE_QUOTES, 'NIFTY', -100);
    expect(result).toBe(BASE_QUOTES);
  });

  it('preserves day_pct and prev_close when updating ltp', () => {
    const result = applyUnderlyingTickLtp(BASE_QUOTES, 'NIFTY', 24200);
    expect(result.NIFTY.day_pct).toBe(0.5);
    expect(result.NIFTY.prev_close).toBe(23880);
    expect(result.NIFTY.ltp).toBe(24200);
  });

  it('only updates the target root — other roots remain unchanged', () => {
    const result = applyUnderlyingTickLtp(BASE_QUOTES, 'NIFTY', 24300);
    // NIFTY updated
    expect(result.NIFTY.ltp).toBe(24300);
    // BANKNIFTY untouched — same nested object reference
    expect(result.BANKNIFTY).toBe(BASE_QUOTES.BANKNIFTY);
    expect(result.BANKNIFTY.ltp).toBe(52000);
  });

  it('returns a new object (not the same reference) when update succeeds', () => {
    const result = applyUnderlyingTickLtp(BASE_QUOTES, 'NIFTY', 24100);
    expect(result).not.toBe(BASE_QUOTES);
    expect(result.NIFTY).not.toBe(BASE_QUOTES.NIFTY);
  });

  it('handles undefined ltp gracefully', () => {
    const result = applyUnderlyingTickLtp(BASE_QUOTES, 'NIFTY', undefined);
    expect(result).toBe(BASE_QUOTES);
  });
});
