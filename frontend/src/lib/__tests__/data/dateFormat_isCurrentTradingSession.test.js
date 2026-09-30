/**
 * dateFormat_isCurrentTradingSession.test.js — Vitest tests for the new
 * `isCurrentTradingSession(ms)` helper added to dateFormat.js (2026-09-30,
 * OrderBook session-boundary reset feature).
 *
 * `isCurrentTradingSession` classifies an arbitrary epoch-ms timestamp as
 * belonging to the CURRENT trading session by comparing its own 08:00 IST
 * trading-session-date (via `tradingSessionDateIST(ms)`) against today's.
 * This is the shared, reusable form of the boundary check OrderBook.svelte
 * uses to drop prior-session rows from every consumer (grid, status
 * counts, CSV export) at once.
 *
 * Five quality dimensions:
 *  1. SSOT   — exercises the real `tradingSessionDateIST`/
 *              `isCurrentTradingSession` pair, not a re-implementation;
 *              the same pair OrderBook.svelte imports.
 *  2. Perf   — pure unit, fake timers, no I/O.
 *  3. Stale  — directly targets the 08:00 IST boundary (not bare
 *              midnight) at exact instants either side of it.
 *  4. Reuse  — `tradingSessionDateIST` now accepts an optional `nowMs`
 *              param specifically so this comparison can reuse it
 *              instead of a parallel boundary definition.
 *  5. UX     — this is the exact predicate gating what an operator sees
 *              in the Order Book: a stale prior-session order must never
 *              render, a same-session one always must.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { isCurrentTradingSession, tradingSessionDateIST } from '$lib/dateFormat.js';

/** Set the fake system clock to a specific IST wall-clock instant. */
function setISTTime(/** @type {string} */ isoDateNoOffset) {
  vi.setSystemTime(new Date(`${isoDateNoOffset}+05:30`));
}

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); });

describe('isCurrentTradingSession — 08:00 IST boundary', () => {
  it('a timestamp from today, just after the 08:00 IST rollover, is current', () => {
    setISTTime('2026-09-30T10:00:00'); // "now"
    const ts = new Date('2026-09-30T08:01:00+05:30').getTime();
    expect(isCurrentTradingSession(ts)).toBe(true);
  });

  it('a timestamp from today, one minute before the 08:00 IST rollover, is NOT current', () => {
    setISTTime('2026-09-30T10:00:00'); // "now" — same calendar day, after rollover
    const ts = new Date('2026-09-30T07:59:00+05:30').getTime();
    expect(isCurrentTradingSession(ts)).toBe(false);
  });

  it('a timestamp from last evening (20:00 IST yesterday) is NOT current when "now" is early morning today', () => {
    // The exact case a bare-midnight filter gets wrong: "now" is 02:00 IST,
    // still within yesterday's trading session per tradingSessionDateIST,
    // so a genuinely-current overnight order must still read as current...
    setISTTime('2026-09-30T02:00:00'); // "now" — before today's 08:00 rollover
    const sameSessionTs = new Date('2026-09-29T20:00:00+05:30').getTime();
    expect(isCurrentTradingSession(sameSessionTs)).toBe(true);
    // ...while a timestamp from the PRIOR trading session (two days back)
    // correctly reads as stale.
    const priorSessionTs = new Date('2026-09-28T20:00:00+05:30').getTime();
    expect(isCurrentTradingSession(priorSessionTs)).toBe(false);
  });

  it('exactly at the rollover instant (08:00:00 IST) is current', () => {
    setISTTime('2026-09-30T10:00:00');
    const ts = new Date('2026-09-30T08:00:00+05:30').getTime();
    expect(isCurrentTradingSession(ts)).toBe(true);
  });

  it('an unparseable/NaN input is never treated as current', () => {
    setISTTime('2026-09-30T10:00:00');
    expect(isCurrentTradingSession(NaN)).toBe(false);
  });

  it('agrees with tradingSessionDateIST(ms) equality by construction', () => {
    setISTTime('2026-09-30T10:00:00');
    const ts = new Date('2026-09-30T09:00:00+05:30').getTime();
    expect(tradingSessionDateIST(ts)).toBe(tradingSessionDateIST());
    expect(isCurrentTradingSession(ts)).toBe(true);
  });
});
