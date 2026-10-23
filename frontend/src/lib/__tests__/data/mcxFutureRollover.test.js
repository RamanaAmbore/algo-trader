/**
 * mcxFutureRollover.test.js — Vitest tests for the 3B fix (2026-10-23):
 * frontend/backend disagreement on the front-month MCX contract on
 * expiry day / overnight.
 *
 * Bug: `findNearestFuture` (instruments.js) and `seedRootMapFromInstruments`
 * (rootOf.js) rolled an MCX future to the next contract at bare midnight
 * of its own expiry date (`r.x > today` / `inst.x <= todayIso`), while the
 * backend's `list_active_futures`/`is_mcx_future_still_current`
 * (`backend/api/algo/expiry_freeze.py`, shipped 2026-10-16) keeps the
 * expiring contract current through its own ENTIRE expiry day and the
 * closed overnight window, rolling only after the next trading day's
 * 08:00 IST open — causing the Snapshot/NavStrip spot basis to flip
 * depending on which contract last ticked.
 *
 * Fix: `isMcxFutureStillCurrent` (dateFormat.js) — a shared predicate
 * mirroring the backend's rule (weekend-aware; no holiday calendar on the
 * frontend, see its own docstring for that residual gap) — wired into
 * both `findNearestFuture` and `seedRootMapFromInstruments`'s MCX branch.
 *
 * Five quality dimensions:
 *  1. SSOT   — exercises the real exported `isMcxFutureStillCurrent`
 *              predicate (not a reimplementation); the `findNearestFuture`
 *              "algorithm" test below calls it directly too, mirroring the
 *              established instruments.js testing convention
 *              (listExpiries.test.js / suggestTradingsymbols.test.js) for
 *              functions that depend on the module-private warm cache.
 *  2. Perf   — pure unit, fake timers, no I/O.
 *  3. Stale  — directly reproduces the reported bug (bare-midnight roll)
 *              and proves the fix (08:00 IST + weekend-aware roll) at
 *              exact boundary instants.
 *  4. Reuse  — one shared predicate used by both files under test.
 *  5. UX     — the exact defect: Snapshot/NavStrip spot flips basis
 *              depending on which contract last ticked.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { isMcxFutureStillCurrent } from '$lib/dateFormat.js';

/** Set the fake system clock to a specific IST wall-clock instant. */
function setISTTime(/** @type {string} */ isoDateNoOffset) {
  vi.setSystemTime(new Date(`${isoDateNoOffset}+05:30`));
}

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); });

describe('isMcxFutureStillCurrent — 08:00 IST rollover (no weekend involved)', () => {
  const EXPIRY = '2026-10-15'; // a Thursday

  it('right after market close on expiry day itself (22:00 IST) — still current', () => {
    setISTTime('2026-10-15T22:00:00');
    expect(isMcxFutureStillCurrent(EXPIRY)).toBe(true);
  });

  it('THE BUG THIS FIXES: just after midnight (00:30 IST the next calendar day) — still current, unlike the old bare `r.x > today` check', () => {
    setISTTime('2026-10-16T00:30:00');
    expect(isMcxFutureStillCurrent(EXPIRY)).toBe(true);
  });

  it('one minute before the next trading day boundary (07:59 IST) — still current', () => {
    setISTTime('2026-10-16T07:59:00');
    expect(isMcxFutureStillCurrent(EXPIRY)).toBe(true);
  });

  it('at the next trading day boundary (08:00 IST, a Friday) — rolls (no longer current)', () => {
    setISTTime('2026-10-16T08:00:00');
    expect(isMcxFutureStillCurrent(EXPIRY)).toBe(false);
  });

  it('well into the next trading day (11:00 IST) — not current', () => {
    setISTTime('2026-10-16T11:00:00');
    expect(isMcxFutureStillCurrent(EXPIRY)).toBe(false);
  });
});

describe('isMcxFutureStillCurrent — weekend-aware rollover (Friday expiry)', () => {
  const FRIDAY_EXPIRY = '2026-10-16'; // a Friday

  it('Saturday morning (09:00 IST) — still current (backend waits for the next TRADING day)', () => {
    setISTTime('2026-10-17T09:00:00');
    expect(isMcxFutureStillCurrent(FRIDAY_EXPIRY)).toBe(true);
  });

  it('Sunday (any time) — still current', () => {
    setISTTime('2026-10-18T15:00:00');
    expect(isMcxFutureStillCurrent(FRIDAY_EXPIRY)).toBe(true);
  });

  it('Monday 07:59 IST — still current (one minute before the real next-open boundary)', () => {
    setISTTime('2026-10-19T07:59:00');
    expect(isMcxFutureStillCurrent(FRIDAY_EXPIRY)).toBe(true);
  });

  it('Monday 08:00 IST — rolls (the real next trading day has opened)', () => {
    setISTTime('2026-10-19T08:00:00');
    expect(isMcxFutureStillCurrent(FRIDAY_EXPIRY)).toBe(false);
  });
});

describe('isMcxFutureStillCurrent — misc', () => {
  it('far-future expiry is always current', () => {
    setISTTime('2026-10-23T12:00:00');
    expect(isMcxFutureStillCurrent('2099-01-01')).toBe(true);
  });

  it('long-past expiry (several months ago, past any weekday boundary) is not current', () => {
    setISTTime('2026-10-23T12:00:00');
    expect(isMcxFutureStillCurrent('2026-06-01')).toBe(false);
  });

  it('null/empty expiry is never current', () => {
    expect(isMcxFutureStillCurrent(null)).toBe(false);
    expect(isMcxFutureStillCurrent('')).toBe(false);
    expect(isMcxFutureStillCurrent(undefined)).toBe(false);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// findNearestFuture (instruments.js) — algorithm-level verification.
//
// `findNearestFuture` reads the module-private `_byUnderlyingType` map,
// populated only via `_buildIndexes()` (called from `loadInstruments()`,
// which talks to IndexedDB + a real API call — not practically mockable
// in this Vitest/Node harness). The established convention for this exact
// file (see listExpiries.test.js, suggestTradingsymbols.test.js) is to
// verify the function's own selection ALGORITHM against a known rows
// array, calling the REAL predicate it depends on (not a reimplementation
// of the risky date-boundary logic — only the trivial "iterate rows,
// return first still-current, else last" shape is inlined here).
// ─────────────────────────────────────────────────────────────────────────────

describe('findNearestFuture — algorithm (MCX branch uses isMcxFutureStillCurrent)', () => {
  /** Mirrors findNearestFuture's exact loop body against a fixture rows array. */
  function pickNearestFuture(rows, today) {
    for (const r of rows) {
      const current = r.e === 'MCX' ? isMcxFutureStillCurrent(r.x) : r.x > today;
      if (current) return r;
    }
    return rows[rows.length - 1];
  }

  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it('stays on the expiring MCX contract overnight (THE BUG) instead of rolling at bare midnight', () => {
    setISTTime('2026-10-16T00:30:00'); // just after midnight, day after expiry
    const rows = [
      { s: 'CRUDEOIL26OCTFUT', e: 'MCX', x: '2026-10-15' }, // expiring contract
      { s: 'CRUDEOIL26NOVFUT', e: 'MCX', x: '2026-11-19' }, // next month
    ];
    const picked = pickNearestFuture(rows, '2026-10-16');
    expect(picked.s).toBe('CRUDEOIL26OCTFUT');
  });

  it('rolls to the next MCX contract once the next trading day 08:00 IST has passed', () => {
    setISTTime('2026-10-16T08:00:00');
    const rows = [
      { s: 'CRUDEOIL26OCTFUT', e: 'MCX', x: '2026-10-15' },
      { s: 'CRUDEOIL26NOVFUT', e: 'MCX', x: '2026-11-19' },
    ];
    const picked = pickNearestFuture(rows, '2026-10-16');
    expect(picked.s).toBe('CRUDEOIL26NOVFUT');
  });

  it('a Friday-expiry MCX contract survives the whole weekend', () => {
    setISTTime('2026-10-18T12:00:00'); // Sunday
    const rows = [
      { s: 'GOLDM26OCTFUT', e: 'MCX', x: '2026-10-16' }, // Friday expiry
      { s: 'GOLDM26NOVFUT', e: 'MCX', x: '2026-11-20' },
    ];
    const picked = pickNearestFuture(rows, '2026-10-18');
    expect(picked.s).toBe('GOLDM26OCTFUT');
  });

  it('a non-MCX (NFO) future on its own expiry day still rolls the OLD way (bare midnight, unchanged)', () => {
    setISTTime('2026-10-16T00:30:00'); // just after midnight, day after expiry
    const today = '2026-10-16';
    const rows = [
      { s: 'NIFTY26OCTFUT', e: 'NFO', x: '2026-10-15' },
      { s: 'NIFTY26NOVFUT', e: 'NFO', x: '2026-11-26' },
    ];
    const picked = pickNearestFuture(rows, today);
    // Old bare `r.x > today` compare: '2026-10-15' > '2026-10-16' is false,
    // so the expiring NFO contract rolls immediately — unchanged behaviour.
    expect(picked.s).toBe('NIFTY26NOVFUT');
  });
});
