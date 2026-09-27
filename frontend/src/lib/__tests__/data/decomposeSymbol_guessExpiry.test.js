/**
 * decomposeSymbol_guessExpiry.test.js — Vitest tests for
 * guessExpiryYmdFromSymbol (2026-09 audit fix, Defect 2 follow-up).
 *
 * A conservative, cache-free expiry-date guess derived purely from a
 * Kite tradingsymbol's own encoded year/month(/day) — used by
 * expiryPnl.js's isExpiredHeldContract as the fallback signal when the
 * instruments cache isn't authoritative for a symbol's root (BFO never
 * fetched, cold start, failed exchange download).
 *
 * Five quality dimensions:
 *  1. SSOT  — reuses decomposeSymbol.js's OWN _OPT_WEEKLY/_OPT_MONTHLY/
 *             _FUT_MONTHLY regexes, not a duplicated parsing mechanism.
 *  2. Perf  — synchronous, no I/O.
 *  3. Stale — guards the conservative-direction contract (never over-
 *             detects expiry within the current/ambiguous month).
 *  4. Reuse — same function used by isExpiredHeldContract; covered here
 *             directly against the exact regex shapes it built on.
 *  5. UX    — leap-year Feb, O/N/D weekly month codes, unparseable inputs.
 */

import { describe, it, expect } from 'vitest';
import { guessExpiryYmdFromSymbol } from '$lib/data/decomposeSymbol.js';

describe('guessExpiryYmdFromSymbol — weekly options (exact day encoded)', () => {
  it('parses a standard weekly option (digit month code)', () => {
    // NIFTY, yy=25, monCode=4 (Apr), dd=24, strike=22000, CE
    expect(guessExpiryYmdFromSymbol('NIFTY2542422000CE')).toBe('2025-04-24');
  });

  it('parses O/N/D month codes (Oct/Nov/Dec — non-digit single-char codes)', () => {
    expect(guessExpiryYmdFromSymbol('NIFTY25O2822000CE')).toBe('2025-10-28');
    expect(guessExpiryYmdFromSymbol('NIFTY25N2722000PE')).toBe('2025-11-27');
    expect(guessExpiryYmdFromSymbol('NIFTY25D2622000CE')).toBe('2025-12-26');
  });
});

describe('guessExpiryYmdFromSymbol — monthly options/futures (last-day-of-month upper bound)', () => {
  it('parses a monthly option to the last day of its named month', () => {
    expect(guessExpiryYmdFromSymbol('RELIANCE25APR2800CE')).toBe('2025-04-30');
  });

  it('parses a monthly future to the last day of its named month', () => {
    expect(guessExpiryYmdFromSymbol('GOLDM26SEPFUT')).toBe('2026-09-30');
  });

  it('leap year: Feb of a leap year resolves to the 29th', () => {
    expect(guessExpiryYmdFromSymbol('NIFTY28FEB22000CE')).toBe('2028-02-29');
  });

  it('non-leap year: Feb resolves to the 28th', () => {
    expect(guessExpiryYmdFromSymbol('NIFTY26FEB22000CE')).toBe('2026-02-28');
  });

  it('conservative direction: a current-month symbol never guesses a date before month-end', () => {
    // This is the whole point of the "last day of month" convention —
    // never mistake a still-live current-month contract for expired.
    const guess = guessExpiryYmdFromSymbol('SENSEX26SEP82000CE');
    expect(guess).toBe('2026-09-30');
    expect(guess >= '2026-09-27').toBe(true); // not before "today" in that scenario
  });
});

describe('guessExpiryYmdFromSymbol — unparseable inputs return null', () => {
  it('null/empty input', () => {
    expect(guessExpiryYmdFromSymbol('')).toBeNull();
    expect(guessExpiryYmdFromSymbol(/** @type {any} */ (null))).toBeNull();
  });

  it('bare equity/index symbol (no F&O shape)', () => {
    expect(guessExpiryYmdFromSymbol('RELIANCE')).toBeNull();
    expect(guessExpiryYmdFromSymbol('NIFTY 50')).toBeNull();
  });

  it('digit-bearing root that decomposeSymbol\'s strict pure-letters regexes reject', () => {
    // Same known limitation documented on decomposeSymbol's weekly regex
    // elsewhere in this codebase (NIFTYNXT50-style roots) — returns null
    // (unknown) rather than a garbage misparse; callers treat null as
    // "not expired", the safe direction.
    expect(guessExpiryYmdFromSymbol('NIFTYNXT5025624400CE')).toBeNull();
  });
});
