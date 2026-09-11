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

// ── _hExpNetTotal formula — Part 3 fix ───────────────────────────────────────
// Verifies the corrected formula: Exp P&L Net = F&O expiry total + equity
// holdings expiry-at-spot total (sum of _hExpByRoot), NOT + holdings lifetime P&L.
//
// The old formula was: _snapshotTotalExp + _hPnlTotal  (semantically wrong)
// The new formula is:  _snapshotTotalExp + sum(_hExpByRoot)  (correct)
//
// For equity holdings, expiry-at-spot = (spot − avg_cost) × qty, which is what
// _hExpByRoot computes.  _hPnlTotal = (ltp − avg_cost) × qty (lifetime, same at
// snapshot time) — but they diverge when there are multiple holdings at
// different avg-cost-to-spot ratios, or when the spot differs from the LTP used
// for lifetime P&L.  This pure-arithmetic test validates the formula pattern.

/**
 * Mirrors the template expression:
 *   _snapshotTotalExp + Object.values(_hExpByRoot).reduce((s, v) => s + (Number(v) || 0), 0)
 *
 * @param {number} snapshotTotalExp - F&O expiry P&L total
 * @param {Record<string, number>} hExpByRoot - per-root equity expiry P&L
 * @returns {number}
 */
function computeHExpNetTotal(snapshotTotalExp, hExpByRoot) {
  return snapshotTotalExp + Object.values(hExpByRoot).reduce((s, v) => s + (Number(v) || 0), 0);
}

/**
 * Old (incorrect) formula for comparison.
 * @param {number} snapshotTotalExp
 * @param {number} hPnlTotal - sum of _hPnlByRoot (lifetime P&L)
 * @returns {number}
 */
function computeHExpNetTotalOld(snapshotTotalExp, hPnlTotal) {
  return snapshotTotalExp + hPnlTotal;
}

describe('_hExpNetTotal — corrected formula (Part 3 fix)', () => {
  it('new formula sums _hExpByRoot values correctly', () => {
    const snapshotTotalExp = 15000;
    const hExpByRoot = { NIFTY: 3000, RELIANCE: -500 };
    const result = computeHExpNetTotal(snapshotTotalExp, hExpByRoot);
    expect(result).toBe(15000 + 3000 + (-500));  // 17500
  });

  it('handles empty _hExpByRoot (no equity holdings)', () => {
    const result = computeHExpNetTotal(20000, {});
    expect(result).toBe(20000);
  });

  it('handles negative F&O expiry total', () => {
    const result = computeHExpNetTotal(-8000, { INFY: 2000 });
    expect(result).toBe(-6000);
  });

  it('handles non-numeric values in _hExpByRoot gracefully via Number() coercion', () => {
    // Simulate a root where the value could be undefined or NaN-ish
    const hExpByRoot = { NIFTY: 5000, BADROOT: /** @type {any} */ (null) };
    const result = computeHExpNetTotal(10000, hExpByRoot);
    // null → Number(null) = 0 → does not add
    expect(result).toBe(15000);
  });

  it('new and old formulas agree when _hExpByRoot sum equals _hPnlTotal (degenerate case)', () => {
    // If equity holdings happen to have pnl = expiry value (e.g. same spot as
    // avg cost → lifetime pnl = 0, expiry = 0 too), both formulas give same result.
    const snapshotTotalExp = 5000;
    const hExpByRoot = { NIFTY: 0, GOLD: 0 };
    const hPnlTotal = 0;
    expect(computeHExpNetTotal(snapshotTotalExp, hExpByRoot))
      .toBe(computeHExpNetTotalOld(snapshotTotalExp, hPnlTotal));
  });

  it('new and old formulas DIVERGE when _hExpByRoot != _hPnlTotal', () => {
    // Holdings intrinsic expiry at a different spot vs lifetime P&L.
    // e.g. RELIANCE: avg=2900, qty=100, current ltp=3050, spot-at-expiry=3000
    //   lifetime pnl = (3050-2900)*100 = 15000
    //   expiry-at-spot = (3000-2900)*100 = 10000
    const snapshotTotalExp = 20000;
    const hExpByRoot = { RELIANCE: 10000 };  // expiry intrinsic
    const hPnlTotal  = 15000;                // lifetime P&L
    const newResult = computeHExpNetTotal(snapshotTotalExp, hExpByRoot);
    const oldResult = computeHExpNetTotalOld(snapshotTotalExp, hPnlTotal);
    expect(newResult).toBe(30000);  // 20000 + 10000
    expect(oldResult).toBe(35000);  // 20000 + 15000
    expect(newResult).not.toBe(oldResult);
  });

  it('multi-underlying case sums all roots', () => {
    const snapshotTotalExp = 0;
    const hExpByRoot = { NIFTY: 10000, BANKNIFTY: 5000, CRUDEOIL: -2000, GOLD: 3000 };
    const result = computeHExpNetTotal(snapshotTotalExp, hExpByRoot);
    expect(result).toBe(16000);  // 0 + 10000 + 5000 - 2000 + 3000
  });
});
