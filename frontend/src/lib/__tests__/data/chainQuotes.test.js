/**
 * chainQuotes.test.js — Vitest unit tests for parseChainQuoteRow.
 *
 * Five quality dimensions:
 *  1. SSOT   — exercises the same module path imported by OptionChainTab.svelte
 *  2. Perf   — all synchronous; no I/O
 *  3. Stale  — guards the depthAvail default (absent/null/undefined → false;
 *              only explicit true → true); backend defaults absent field to false
 *  4. Reuse  — parseChainQuoteRow is the single parse boundary for chain data
 *  5. UX     — visual "(L)" indicator is driven by depthAvail; these tests
 *              verify the flag is set correctly so the indicator fires when it
 *              should. Full rendering coverage requires Playwright.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { parseChainQuoteRow } from '$lib/data/chainQuotes.js';

// ─────────────────────────────────────────────────────────────────────────────
// Fixture helpers
// ─────────────────────────────────────────────────────────────────────────────

/** Minimal row with both sides explicitly provided. */
function makeRow(overrides = {}) {
  return {
    k:                  '24000',
    ce_bid:             120.5,
    ce_ask:             121.0,
    ce_sym:             'NIFTY24AUG24000CE',
    ce_ls:              50,
    pe_bid:             80.25,
    pe_ask:             80.75,
    pe_sym:             'NIFTY24AUG24000PE',
    pe_ls:              50,
    exchange:           'NFO',
    ce_depth_available: true,
    pe_depth_available: true,
    ...overrides,
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Return shape
// ─────────────────────────────────────────────────────────────────────────────

describe('parseChainQuoteRow — return shape', () => {
  it('returns a [key, quote] tuple', () => {
    const [key, q] = parseChainQuoteRow(makeRow());
    expect(key).toBe('24000');
    expect(q).toHaveProperty('ce');
    expect(q).toHaveProperty('pe');
  });

  it('parses CE bid/ask/sym/ls/exchange correctly', () => {
    const [, q] = parseChainQuoteRow(makeRow());
    expect(q.ce.bid).toBe(120.5);
    expect(q.ce.ask).toBe(121.0);
    expect(q.ce.sym).toBe('NIFTY24AUG24000CE');
    expect(q.ce.ls).toBe(50);
    expect(q.ce.exchange).toBe('NFO');
  });

  it('parses PE bid/ask/sym/ls/exchange correctly', () => {
    const [, q] = parseChainQuoteRow(makeRow());
    expect(q.pe.bid).toBe(80.25);
    expect(q.pe.ask).toBe(80.75);
    expect(q.pe.sym).toBe('NIFTY24AUG24000PE');
    expect(q.pe.ls).toBe(50);
    expect(q.pe.exchange).toBe('NFO');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// depthAvail — the flag that drives the "(L)" visual indicator
// ─────────────────────────────────────────────────────────────────────────────

describe('parseChainQuoteRow — depthAvail flag', () => {
  it('sets depthAvail=true when backend sends true for both sides', () => {
    const [, q] = parseChainQuoteRow(makeRow({ ce_depth_available: true, pe_depth_available: true }));
    expect(q.ce.depthAvail).toBe(true);
    expect(q.pe.depthAvail).toBe(true);
  });

  it('sets depthAvail=false when backend sends false for CE (illiquid far-OTM call)', () => {
    const [, q] = parseChainQuoteRow(makeRow({ ce_depth_available: false }));
    expect(q.ce.depthAvail).toBe(false);
    // PE side unaffected
    expect(q.pe.depthAvail).toBe(true);
  });

  it('sets depthAvail=false when backend sends false for PE (illiquid far-OTM put)', () => {
    const [, q] = parseChainQuoteRow(makeRow({ pe_depth_available: false }));
    expect(q.pe.depthAvail).toBe(false);
    // CE side unaffected
    expect(q.ce.depthAvail).toBe(true);
  });

  it('sets depthAvail=false for both sides when both are illiquid', () => {
    const [, q] = parseChainQuoteRow(makeRow({ ce_depth_available: false, pe_depth_available: false }));
    expect(q.ce.depthAvail).toBe(false);
    expect(q.pe.depthAvail).toBe(false);
  });

  // SSOT: absent field → false. Backend defaults absent ce/pe_depth_available to
  // false (no depth confirmed). Only an explicit `true` from the backend means
  // depth is available. The "(L)" indicator should fire on absent field.
  it('defaults depthAvail=false when the field is absent (backend did not confirm depth)', () => {
    const row = makeRow();
    delete row.ce_depth_available;
    delete row.pe_depth_available;
    const [, q] = parseChainQuoteRow(row);
    expect(q.ce.depthAvail).toBe(false);
    expect(q.pe.depthAvail).toBe(false);
  });

  it('defaults depthAvail=false when the field is null (broker returned null)', () => {
    const [, q] = parseChainQuoteRow(makeRow({ ce_depth_available: null, pe_depth_available: null }));
    expect(q.ce.depthAvail).toBe(false);
    expect(q.pe.depthAvail).toBe(false);
  });

  it('defaults depthAvail=false when the field is undefined', () => {
    const [, q] = parseChainQuoteRow(makeRow({ ce_depth_available: undefined, pe_depth_available: undefined }));
    expect(q.ce.depthAvail).toBe(false);
    expect(q.pe.depthAvail).toBe(false);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Null / missing numeric fields
// ─────────────────────────────────────────────────────────────────────────────

describe('parseChainQuoteRow — null/missing numerics', () => {
  it('preserves null bid/ask when backend sends null', () => {
    const [, q] = parseChainQuoteRow(makeRow({ ce_bid: null, ce_ask: null }));
    expect(q.ce.bid).toBeNull();
    expect(q.ce.ask).toBeNull();
  });

  it('preserves null sym when backend sends no ce_sym', () => {
    const row = makeRow();
    delete row.ce_sym;
    const [, q] = parseChainQuoteRow(row);
    expect(q.ce.sym).toBeNull();
  });

  it('converts string numerics to Number', () => {
    const [, q] = parseChainQuoteRow(makeRow({ ce_bid: '99.5', ce_ask: '100' }));
    expect(q.ce.bid).toBe(99.5);
    expect(q.ce.ask).toBe(100);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Exchange fallback
// ─────────────────────────────────────────────────────────────────────────────

describe('parseChainQuoteRow — exchange fallback', () => {
  it('uses row.exchange when present', () => {
    const [, q] = parseChainQuoteRow(makeRow({ exchange: 'BSE' }));
    expect(q.ce.exchange).toBe('BSE');
    expect(q.pe.exchange).toBe('BSE');
  });

  it('falls back to the exchange argument when row.exchange is absent', () => {
    const row = makeRow();
    delete row.exchange;
    const [, q] = parseChainQuoteRow(row, 'MCX');
    expect(q.ce.exchange).toBe('MCX');
    expect(q.pe.exchange).toBe('MCX');
  });

  it('returns empty string when neither row.exchange nor argument is provided', () => {
    const row = makeRow();
    delete row.exchange;
    const [, q] = parseChainQuoteRow(row);
    expect(q.ce.exchange).toBe('');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Wide-spread threshold — drives the ⚠ indicator in OptionChainTab
// Formula: (ask - bid) / ((ask + bid) / 2) > 0.10
// ─────────────────────────────────────────────────────────────────────────────

describe('spread-wide threshold', () => {
  /** Utility: evaluate the spread-wide formula directly (mirrors OptionChainTab). */
  function isSpreadWide(bid, ask) {
    return bid > 0 && ask > 0 && (ask - bid) / ((ask + bid) / 2) > 0.10;
  }

  it('flags a wide spread: bid=10, ask=12 → ~18.2% spread → wide', () => {
    expect(isSpreadWide(10, 12)).toBe(true);
  });

  it('does not flag a narrow spread: bid=100, ask=101 → ~1% spread → not wide', () => {
    expect(isSpreadWide(100, 101)).toBe(false);
  });

  it('does not flag when bid=0 (no live bid): bid=0, ask=5 → not wide (bid guard fails)', () => {
    expect(isSpreadWide(0, 5)).toBe(false);
  });

  it('does not flag when both bid and ask are 0', () => {
    expect(isSpreadWide(0, 0)).toBe(false);
  });

  it('does not flag when spread is exactly 10% (threshold is strictly >0.10)', () => {
    // bid=100, ask=111.111…: spread/mid = 10/105.55... ≈ 9.47% — below threshold
    // Use exact boundary: mid = (90+110)/2 = 100, spread = 20, ratio = 0.20 → wide
    // At exactly 10%: bid=9.524, ask=10.526 → (1.002/10.025) = 0.0999... → not wide
    expect(isSpreadWide(9.524, 10.526)).toBe(false);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Button disable condition — drives disabled attr on CE/PE +/- buttons
// Formula: !(ceQ?.bid > 0 || ceQ?.ask > 0)
// ─────────────────────────────────────────────────────────────────────────────

describe('button disable condition', () => {
  /** Utility: evaluate the disable formula directly (mirrors OptionChainTab). */
  function isDisabled(ceQ) {
    return !(ceQ?.bid > 0 || ceQ?.ask > 0);
  }

  it('disables when quote is null (no data for this strike)', () => {
    expect(isDisabled(null)).toBe(true);
  });

  it('disables when both bid and ask are 0 (illiquid, no market)', () => {
    expect(isDisabled({ bid: 0, ask: 0 })).toBe(true);
  });

  it('enables when bid > 0, ask = 0 (one-sided market — sell side only)', () => {
    expect(isDisabled({ bid: 5, ask: 0 })).toBe(false);
  });

  it('enables when bid = 0, ask > 0 (one-sided market — buy side only)', () => {
    expect(isDisabled({ bid: 0, ask: 5 })).toBe(false);
  });

  it('enables when both bid and ask are positive (normal two-sided market)', () => {
    expect(isDisabled({ bid: 10, ask: 12 })).toBe(false);
  });

  it('disables when quote is undefined (chainQuotesMap had no entry)', () => {
    expect(isDisabled(undefined)).toBe(true);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// _refreshChainQuotes timeout/abort pattern (SSOT: OptionChainTab.svelte)
//
// The component attaches a 10-second AbortController timeout to every chain-
// quotes fetch so that _pricesFetching is always reset — even when the backend
// hangs (e.g. broker session inactive on weekends). This suite verifies the
// raw AbortController + setTimeout + Promise pattern behaves correctly without
// needing to import the Svelte component.
//
// Quality dimensions covered:
//  1. SSOT   — mirrors the exact pattern used in _refreshChainQuotes()
//  2. Perf   — fake timers; no real I/O or 10-second wall-clock wait
//  3. Stale  — abort fires even when fetch never resolves (hanging backend)
//  4. Reuse  — AbortController abort() is idempotent; double-abort is safe
//  5. UX     — _pricesFetching reset ensures the spinner clears after timeout
// ─────────────────────────────────────────────────────────────────────────────

describe('_refreshChainQuotes — abort timeout pattern', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /**
   * Replicates the core timeout pattern from _refreshChainQuotes() without
   * importing the Svelte component. The fetch mock returns a promise that
   * never resolves (simulates a hanging backend).
   */
  function runPattern(fetchMock) {
    let pricesFetching = false;
    const ac = new AbortController();

    pricesFetching = true;
    const tout = setTimeout(() => ac.abort(), 10_000);

    return fetchMock(ac.signal)
      .catch(() => {})
      .finally(() => {
        clearTimeout(tout);
        pricesFetching = false;
      })
      .then(() => pricesFetching); // resolve with final value for assertion
  }

  it('resets pricesFetching=false after 10s when fetch never resolves', async () => {
    // fetch that never resolves but rejects when the signal is aborted
    const fetchMock = (signal) =>
      new Promise((_resolve, reject) => {
        signal.addEventListener('abort', () => reject(new DOMException('AbortError', 'AbortError')));
      });

    const p = runPattern(fetchMock);

    // Before timeout fires, pricesFetching is still true (promise pending)
    // Advance fake clock past the 10-second threshold
    vi.advanceTimersByTime(10_001);

    const finalValue = await p;
    expect(finalValue).toBe(false);
  });

  it('does NOT fire the abort if the fetch resolves before 10s', async () => {
    let aborted = false;
    const fetchMock = (signal) => {
      signal.addEventListener('abort', () => { aborted = true; });
      return Promise.resolve({ rows: [], exchange: 'NFO' });
    };

    const p = runPattern(fetchMock);
    const finalValue = await p;

    // Advance timers after fetch already resolved — abort should not fire
    vi.advanceTimersByTime(10_001);

    expect(finalValue).toBe(false);
    expect(aborted).toBe(false);
  });

  it('AbortController.abort() is idempotent — double-abort does not throw', () => {
    const ac = new AbortController();
    expect(() => { ac.abort(); ac.abort(); }).not.toThrow();
    expect(ac.signal.aborted).toBe(true);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// _netAgainstBasket netting logic (SSOT: OptionChainTab.svelte)
//
// _netAgainstBasket is a closure inside the Svelte component and is not
// directly importable. This suite replicates the arithmetic as a pure local
// helper and exercises all four branches:
//   • opposite leg found, newLots <= 0  → leg removed (filter path)
//   • opposite leg found, newLots >  0  → leg kept with decremented lots (map path)
//   • no opposite leg found             → returns false (no netting)
//
// Quality dimensions:
//  1. SSOT   — mirrors the exact condition `newLots <= 0` used in the component
//  2. Perf   — pure arithmetic; no I/O, no Svelte runtime
//  3. Stale  — guards the `lots || 1` default so a missing lots field still nets
//  4. Reuse  — shared netting arithmetic is tested once here, not per-consumer
//  5. UX     — correct netting prevents duplicate/opposing legs in the basket
// ─────────────────────────────────────────────────────────────────────────────

describe('_netAgainstBasket netting logic', () => {
  /**
   * Pure replication of the _netAgainstBasket arithmetic from OptionChainTab.svelte.
   * Returns { netted, basket } where `netted` mirrors the boolean return value and
   * `basket` is the resulting array (using only the local/else path for simplicity).
   *
   * @param {Array<{sym:string, side:string, lots:number}>} basket
   * @param {string} sym
   * @param {'BUY'|'SELL'} sideTag
   */
  function netAgainstBasket(basket, sym, sideTag) {
    const oppSide = sideTag === 'BUY' ? 'SELL' : 'BUY';
    const idx = basket.findIndex(b => b.sym === sym && b.side === oppSide);
    if (idx < 0) return { netted: false, basket };
    const leg = basket[idx];
    const newLots = (leg.lots || 1) - 1;
    let next;
    if (newLots <= 0) {
      next = basket.filter((_, i) => i !== idx);
    } else {
      next = basket.map((b, i) => i === idx ? { ...b, lots: newLots } : b);
    }
    return { netted: true, basket: next };
  }

  it('BUY against existing SELL 1 lot → newLots=0 → leg removed', () => {
    const basket = [{ sym: 'NIFTY25JAN24000CE', side: 'SELL', lots: 1 }];
    const { netted, basket: next } = netAgainstBasket(basket, 'NIFTY25JAN24000CE', 'BUY');
    expect(netted).toBe(true);
    expect(next).toHaveLength(0);
  });

  it('BUY against existing SELL 2 lots → newLots=1 → leg kept with lots=1', () => {
    const basket = [{ sym: 'NIFTY25JAN24000CE', side: 'SELL', lots: 2 }];
    const { netted, basket: next } = netAgainstBasket(basket, 'NIFTY25JAN24000CE', 'BUY');
    expect(netted).toBe(true);
    expect(next).toHaveLength(1);
    expect(next[0].lots).toBe(1);
  });

  it('SELL against existing BUY 1 lot → leg removed', () => {
    const basket = [{ sym: 'BANKNIFTY25JAN48000PE', side: 'BUY', lots: 1 }];
    const { netted, basket: next } = netAgainstBasket(basket, 'BANKNIFTY25JAN48000PE', 'SELL');
    expect(netted).toBe(true);
    expect(next).toHaveLength(0);
  });

  it('no opposite leg in basket → returns false, basket unchanged', () => {
    const basket = [{ sym: 'NIFTY25JAN24000CE', side: 'BUY', lots: 1 }];
    const { netted, basket: next } = netAgainstBasket(basket, 'NIFTY25JAN24000CE', 'BUY');
    expect(netted).toBe(false);
    expect(next).toHaveLength(1);
  });

  it('missing lots field defaults to 1 → removal on first net', () => {
    // lots intentionally absent to test the (leg.lots || 1) fallback
    const basket = /** @type {any} */ ([{ sym: 'NIFTY25JAN24000CE', side: 'SELL' }]);
    const { netted, basket: next } = netAgainstBasket(basket, 'NIFTY25JAN24000CE', 'BUY');
    expect(netted).toBe(true);
    expect(next).toHaveLength(0);
  });

  it('netting is sym-specific — does not remove leg for a different symbol', () => {
    const basket = [{ sym: 'NIFTY25JAN23000CE', side: 'SELL', lots: 1 }];
    const { netted } = netAgainstBasket(basket, 'NIFTY25JAN24000CE', 'BUY');
    expect(netted).toBe(false);
  });
});
