import { describe, it, expect } from 'vitest';
import { applyUnderlyingTickLtp } from '$lib/data/underlyingQuoteUtils.js';
import { resolveUnderlying } from '$lib/data/resolveUnderlying.js';

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

// ── Bug A: anchor bridge for MCX — tickBus fires on anchor tradingsymbol ──────
// When strategy.spot_anchor_contract = "CRUDEOILSEP26FUT" and the root key in
// _underlyingQuotes is "CRUDEOIL", applyUnderlyingTickLtp must be called with
// the root ("CRUDEOIL"), not the anchor tradingsymbol. This test verifies the
// function correctly updates _underlyingQuotes["CRUDEOIL"].ltp when called that
// way, ensuring the tickBus bridge fix propagates live ticks to the payoff chart.
describe('anchor bridge — MCX root key differs from anchor tradingsymbol', () => {
  const MCX_QUOTES = {
    CRUDEOIL: { ltp: 5800, day_pct: 0.4, prev_close: 5780 },
    GOLD:     { ltp: 74000, day_pct: -0.1, prev_close: 74074 },
  };

  it('updates CRUDEOIL ltp when anchor tick arrives with stratUnd = "CRUDEOIL"', () => {
    // Simulates: root === _anchor ("CRUDEOILSEP26FUT"), _stratUnd = "CRUDEOIL"
    // The fix calls applyUnderlyingTickLtp(_underlyingQuotes, _stratUnd, ltp)
    const result = applyUnderlyingTickLtp(MCX_QUOTES, 'CRUDEOIL', 5850);
    expect(result.CRUDEOIL.ltp).toBe(5850);
    expect(result).not.toBe(MCX_QUOTES);
  });

  it('returns same reference when stratUnd is not in _underlyingQuotes', () => {
    // e.g. a strategy whose underlying is not yet loaded
    const result = applyUnderlyingTickLtp(MCX_QUOTES, 'NATURALGAS', 320);
    expect(result).toBe(MCX_QUOTES);
  });

  it('only updates the targeted MCX root — GOLD remains unchanged', () => {
    const result = applyUnderlyingTickLtp(MCX_QUOTES, 'CRUDEOIL', 5900);
    expect(result.GOLD).toBe(MCX_QUOTES.GOLD);
    expect(result.GOLD.ltp).toBe(74000);
  });
});

// ── Bug B: _clientPayoffStub spot fallback resolves futures tradingsymbol ─────
// For MCX underlyings selectedUnderlying = "CRUDEOIL" but symbolStore is keyed
// by tradingsymbol "CRUDEOILSEP26FUT". The fix resolves the futures tradingsymbol
// via resolveUnderlying before calling getSnapshot. This test verifies that
// resolveUnderlying("CRUDEOIL", findNearestFuture) returns the futures
// tradingsymbol so _lookupSym is "CRUDEOILSEP26FUT" rather than "CRUDEOIL".
describe('_clientPayoffStub spot fallback — MCX resolves futures tradingsymbol', () => {
  // Stub for findNearestFuture: mirrors instruments.js for a single MCX contract.
  const stubFindNearestFuture = (/** @type {string} */ underlying) => {
    if (underlying === 'CRUDEOIL') return { s: 'CRUDEOILSEP26FUT', e: 'MCX' };
    if (underlying === 'GOLD')     return { s: 'GOLDSEP26FUT',      e: 'MCX' };
    return null;
  };

  it('resolves CRUDEOIL → tradingsymbol "CRUDEOILSEP26FUT"', () => {
    const resolved = resolveUnderlying('CRUDEOIL', stubFindNearestFuture);
    expect(resolved?.tradingsymbol).toBe('CRUDEOILSEP26FUT');
  });

  it('_lookupSym uses resolved tradingsymbol, not bare root "CRUDEOIL"', () => {
    // Mirrors the fix: _resolvedSym || String(_sel).toUpperCase()
    const _sel = 'CRUDEOIL';
    const _resolvedSym = resolveUnderlying(String(_sel).toUpperCase(), stubFindNearestFuture)?.tradingsymbol;
    const _lookupSym = _resolvedSym || String(_sel).toUpperCase();
    expect(_lookupSym).toBe('CRUDEOILSEP26FUT');
    expect(_lookupSym).not.toBe('CRUDEOIL');
  });

  it('falls back to bare root when resolveUnderlying returns null', () => {
    // e.g. instruments cache cold and CDS currency with no future
    const _sel = 'USDINR';
    // Stub returns null for USDINR (simulates cold cache)
    const _resolvedSym = resolveUnderlying(String(_sel).toUpperCase(), () => null)?.tradingsymbol;
    // resolveUnderlying for CDS with null fut returns null entirely → _resolvedSym = undefined
    const _lookupSym = _resolvedSym || String(_sel).toUpperCase();
    expect(_lookupSym).toBe('USDINR');
  });

  it('NSE index NIFTY resolves to spot tradingsymbol "NIFTY 50"', () => {
    const resolved = resolveUnderlying('NIFTY', stubFindNearestFuture);
    // For index underlyings, resolveUnderlying returns the Kite spot key
    expect(resolved?.tradingsymbol).toBe('NIFTY 50');
    // _lookupSym uses this key → matches symbolStore which keys on Kite quote-keys
    const _lookupSym = resolved?.tradingsymbol || 'NIFTY';
    expect(_lookupSym).toBe('NIFTY 50');
  });
});
