/**
 * expiryPnl_sessionBoundary.test.js — Vitest tests for the 08:00 IST
 * trading-session boundary fix to `isExpiredHeldContract` (2026-09-27).
 *
 * Bug: `isExpiredHeldContract` compared a held contract's expiry date
 * against `todayIST()` — a bare midnight-IST calendar-date rollover.
 * Operator report: Exp P&L (payoff chart, Legs grid, Snapshot grid,
 * NavStrip's P-pill — every consumer of this predicate via
 * `expiredLegFrozenPnl`) was collapsing to the frozen ACTUAL/settled P&L
 * right after midnight following expiry day — several hours before the
 * next trading session opens (~09:15 IST), and the operator's explicit
 * requirement is that Exp P&L stays a THEORETICAL mark-to-spot value all
 * the way through expiry-day close and the entire overnight closed window,
 * only flipping once the next trading session has actually begun.
 *
 * Fix: swap the comparison to `tradingSessionDateIST()` (08:00 IST
 * rollover, dateFormat.js), so the "is this contract expired" decision
 * only flips once the NEXT session opens, not at bare midnight.
 *
 * Unlike expiryPnl_expired.test.js (which mocks dateFormat.js entirely to
 * pin a fixed date for its own unrelated branch-coverage tests), this file
 * deliberately does NOT mock dateFormat.js — it exercises the REAL
 * `tradingSessionDateIST()` against a fake system clock, so the actual
 * 08:00 IST rollover arithmetic is under test, not just the calling
 * convention.
 *
 * Five quality dimensions:
 *  1. SSOT   — tests the real tradingSessionDateIST() + the real
 *              isExpiredHeldContract() call site that consumes it,
 *              not a re-implementation.
 *  2. Perf   — pure unit, fake timers, no I/O.
 *  3. Stale  — directly reproduces the reported bug (midnight rollover)
 *              and proves the fix (08:00 IST rollover) at exact boundary
 *              instants, not just "some time later".
 *  4. Reuse  — isExpiredHeldContract's injected-lookup DI pattern, same
 *              as expiryPnl_expired.test.js.
 *  5. UX     — this is the exact defect the operator observed live:
 *              Exp P&L must not equal actual P&L overnight on expiry day.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { tradingSessionDateIST } from '$lib/dateFormat.js';

const _mockCache = {
  byInst: /** @type {Record<string, {x?:string}>} */ ({}),
  fnoRoots: /** @type {Set<string>} */ (new Set()),
};
vi.mock('$lib/data/instruments.js', () => ({
  getInstrument: (/** @type {string} */ sym) => _mockCache.byInst[sym] ?? null,
  hasFNO: (/** @type {string} */ root) => _mockCache.fnoRoots.has(root),
}));

import { isExpiredHeldContract } from '$lib/data/expiryPnl.js';

beforeEach(() => {
  _mockCache.byInst = {};
  _mockCache.fnoRoots = new Set();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

/** Set the fake system clock to a specific IST wall-clock instant. */
function setISTTime(/** @type {string} */ isoDateNoOffset) {
  vi.setSystemTime(new Date(`${isoDateNoOffset}+05:30`));
}

describe('tradingSessionDateIST — 08:00 IST rollover', () => {
  it('one minute before rollover (07:59 IST) still reads the previous day', () => {
    setISTTime('2026-09-28T07:59:00');
    expect(tradingSessionDateIST()).toBe('2026-09-27');
  });

  it('exactly at rollover (08:00:00 IST) reads the new day', () => {
    setISTTime('2026-09-28T08:00:00');
    expect(tradingSessionDateIST()).toBe('2026-09-28');
  });

  it('just after midnight (00:30 IST) still reads the PREVIOUS day — the exact case the bug fix targets', () => {
    setISTTime('2026-09-28T00:30:00');
    expect(tradingSessionDateIST()).toBe('2026-09-27');
  });

  it('mid-afternoon (14:00 IST) reads the current day, same as todayIST would', () => {
    setISTTime('2026-09-27T14:00:00');
    expect(tradingSessionDateIST()).toBe('2026-09-27');
  });
});

describe('isExpiredHeldContract — stays NOT-expired through the whole overnight closed window on expiry day', () => {
  const EXPIRING_SYM = 'NIFTY26SEP24000CE';

  beforeEach(() => {
    // Contract expires 2026-09-27 (today, in every scenario below) and is
    // still present in the instruments cache with its own expiry date —
    // the AUTHORITATIVE branch.
    _mockCache.byInst = { [EXPIRING_SYM]: { x: '2026-09-27' } };
  });

  it('right after market close on expiry day itself (20:00 IST) — NOT expired', () => {
    setISTTime('2026-09-27T20:00:00');
    expect(isExpiredHeldContract(EXPIRING_SYM, 1)).toBe(false);
  });

  it('THE BUG THIS FIXES: just after midnight (00:30 IST the next calendar day) — still NOT expired, unlike the old todayIST()-based check', () => {
    setISTTime('2026-09-28T00:30:00');
    expect(isExpiredHeldContract(EXPIRING_SYM, 1)).toBe(false);
  });

  it('05:00 IST the next morning, still well before market open — still NOT expired', () => {
    setISTTime('2026-09-28T05:00:00');
    expect(isExpiredHeldContract(EXPIRING_SYM, 1)).toBe(false);
  });

  it('one minute before the next session boundary (07:59 IST) — still NOT expired', () => {
    setISTTime('2026-09-28T07:59:00');
    expect(isExpiredHeldContract(EXPIRING_SYM, 1)).toBe(false);
  });

  it('at the next trading session boundary (08:00 IST) — NOW expired', () => {
    setISTTime('2026-09-28T08:00:00');
    expect(isExpiredHeldContract(EXPIRING_SYM, 1)).toBe(true);
  });

  it('well into the next trading day (11:00 IST) — expired', () => {
    setISTTime('2026-09-28T11:00:00');
    expect(isExpiredHeldContract(EXPIRING_SYM, 1)).toBe(true);
  });
});
