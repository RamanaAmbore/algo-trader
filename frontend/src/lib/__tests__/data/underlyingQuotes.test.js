import { describe, it, expect } from 'vitest';
import { applyUnderlyingTickLtp } from '$lib/data/underlyingQuoteUtils.js';

// ── prevClose priority logic (tab-switch garble fix) ─────────────────────────
// Mirrors the _prevClose derived in +page.svelte:
//   if (strategy?.spot_prev_close > 0) → use strategy.spot_prev_close
//   else → _underlyingQuotes[selectedUnderlying]?.prev_close ?? null
//
// This is gated by _throttledTick + untrack() in the real component to prevent
// OptionsPayoff SVG re-renders on every _underlyingQuotes wholesale replacement.

/**
 * @param {{ spot_prev_close?: number|null } | null} strategy
 * @param {Record<string, { prev_close?: number }>} underlyingQuotes
 * @param {string} selectedUnderlying
 * @returns {number|null}
 */
function computePrevClose(strategy, underlyingQuotes, selectedUnderlying) {
  if ((strategy?.spot_prev_close ?? 0) > 0) return strategy.spot_prev_close;
  return underlyingQuotes[selectedUnderlying]?.prev_close ?? null;
}

describe('_prevClose priority logic (derivatives page)', () => {
  it('strategy.spot_prev_close > 0 takes priority over underlyingQuotes', () => {
    const result = computePrevClose(
      { spot_prev_close: 24000 },
      { NIFTY: { prev_close: 23500 } },
      'NIFTY',
    );
    expect(result).toBe(24000);
  });

  it('falls back to underlyingQuotes when strategy.spot_prev_close is 0', () => {
    const result = computePrevClose(
      { spot_prev_close: 0 },
      { NIFTY: { prev_close: 23500 } },
      'NIFTY',
    );
    expect(result).toBe(23500);
  });

  it('falls back to underlyingQuotes when strategy is null', () => {
    const result = computePrevClose(
      null,
      { NIFTY: { prev_close: 23880 } },
      'NIFTY',
    );
    expect(result).toBe(23880);
  });

  it('returns null when strategy has no prev_close and underlyingQuotes is empty', () => {
    const result = computePrevClose(null, {}, 'NIFTY');
    expect(result).toBe(null);
  });

  it('returns null when selectedUnderlying not in underlyingQuotes', () => {
    const result = computePrevClose(
      { spot_prev_close: 0 },
      { BANKNIFTY: { prev_close: 52000 } },
      'NIFTY',
    );
    expect(result).toBe(null);
  });

  it('strategy.spot_prev_close negative → falls through to underlyingQuotes', () => {
    const result = computePrevClose(
      { spot_prev_close: -1 },
      { NIFTY: { prev_close: 23880 } },
      'NIFTY',
    );
    expect(result).toBe(23880);
  });
});

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

  // Fix B: same-value guard — must return SAME reference (toBe, not toEqual)
  // so that Fix C's `if (_next !== _underlyingQuotes)` assignment guard works.
  it('returns the SAME object reference when ltp is identical to current value', () => {
    const quotes = { NIFTY: { ltp: 24000, day_pct: 0.5, prev_close: 23880 } };
    const result = applyUnderlyingTickLtp(quotes, 'NIFTY', 24000);
    expect(result).toBe(quotes);          // reference equality — no new allocation
    expect(result.NIFTY).toBe(quotes.NIFTY); // nested object also same reference
  });

  it('returns a NEW object reference when ltp differs by even 0.05', () => {
    const quotes = { NIFTY: { ltp: 24000, day_pct: 0.5, prev_close: 23880 } };
    const result = applyUnderlyingTickLtp(quotes, 'NIFTY', 24000.05);
    expect(result).not.toBe(quotes);
    expect(result.NIFTY.ltp).toBe(24000.05);
  });
});
