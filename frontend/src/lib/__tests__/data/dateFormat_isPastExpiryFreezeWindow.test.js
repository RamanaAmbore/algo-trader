/**
 * dateFormat_isPastExpiryFreezeWindow.test.js — Vitest tests for the
 * 2026-11-27 weekend-boundary fix.
 *
 * Confirmed live incident: every caller that compared a held F&O
 * contract's expiry date directly against `tradingSessionDateIST()`
 * (`expiryIso < sessionToday`) rolled the "is this contract expired/
 * frozen" decision at the very FIRST Saturday 08:00 IST session-boundary
 * tick after a Friday expiry — TWO FULL DAYS before the real next-
 * trading-day boundary (Monday 08:00 IST). Concretely: a real GOLD26NOV
 * contract expiring Friday 2026-11-27 would have flipped to "expired/
 * frozen" at Saturday 2026-11-28 08:00 IST, instead of Monday 2026-11-30
 * 08:00 IST.
 *
 * `isPastExpiryFreezeWindow(expiryIso, sessionToday)` is the new,
 * exchange-agnostic, weekend-aware replacement for that bare compare.
 * `lastWeekdayIsoBefore(dateIso)` is its symmetric sibling to
 * `isMcxFutureStillCurrent`'s existing `_firstWeekdayIsoAfter`, used by
 * `isExpiredHeldContract`'s root-authoritative cache-miss branch
 * (expiryPnl.js) to tighten an otherwise-unreliable symbol-parsed guess.
 *
 * Five quality dimensions:
 *  1. SSOT   — tests the real exported functions directly, with
 *              explicit `sessionToday` arguments (no fake-timer
 *              dependency needed, since both take the session date as
 *              a plain string parameter).
 *  2. Perf   — pure unit, no I/O, no timers.
 *  3. Stale  — directly reproduces the reported bug (Friday expiry,
 *              Saturday/Sunday false-positive) and proves the fix at
 *              exact boundary instants, not just "some time later".
 *  4. Reuse  — `isMcxFutureStillCurrent` is re-expressed as a thin
 *              negation of this function; a dedicated test pins that
 *              byte-for-byte equivalence so the two can never drift.
 *  5. UX     — this is the exact defect behind the 2026-11-27 incident:
 *              Exp P&L (Payoff chart, Legs grid, Snapshot grid, NavStrip
 *              P-pill) must not collapse to frozen/settled two days
 *              early over a weekend.
 */

import { describe, it, expect } from 'vitest';
import {
  isPastExpiryFreezeWindow,
  lastWeekdayIsoBefore,
  isMcxFutureStillCurrent,
  tradingSessionDateIST,
} from '$lib/dateFormat.js';

describe('isPastExpiryFreezeWindow — Friday expiry, weekend grace window', () => {
  const FRIDAY_EXPIRY = '2026-11-27'; // confirmed Friday

  it('expiry day itself — not past the freeze window', () => {
    expect(isPastExpiryFreezeWindow(FRIDAY_EXPIRY, '2026-11-27')).toBe(false);
  });

  it('THE BUG THIS FIXES: Saturday (the very next session date) — still NOT past the freeze window', () => {
    expect(isPastExpiryFreezeWindow(FRIDAY_EXPIRY, '2026-11-28')).toBe(false);
  });

  it('Sunday — still NOT past the freeze window', () => {
    expect(isPastExpiryFreezeWindow(FRIDAY_EXPIRY, '2026-11-29')).toBe(false);
  });

  it('Monday (the next real trading day) — NOW past the freeze window', () => {
    expect(isPastExpiryFreezeWindow(FRIDAY_EXPIRY, '2026-11-30')).toBe(true);
  });

  it('well into the following week — past the freeze window', () => {
    expect(isPastExpiryFreezeWindow(FRIDAY_EXPIRY, '2026-12-02')).toBe(true);
  });

  it('a future expiry date — never past the freeze window', () => {
    expect(isPastExpiryFreezeWindow('2026-12-04', '2026-11-27')).toBe(false);
  });

  it('null/undefined expiry — not past the freeze window (fail-open)', () => {
    expect(isPastExpiryFreezeWindow(null, '2026-11-28')).toBe(false);
    expect(isPastExpiryFreezeWindow(undefined, '2026-11-28')).toBe(false);
  });

  it('a Mon-Thu expiry has NO weekend gap — flips the very next calendar day', () => {
    // Thursday expiry -> Friday is already the next weekday, no skip needed.
    expect(isPastExpiryFreezeWindow('2026-11-26', '2026-11-27')).toBe(true);
  });

  it('defaults sessionToday to tradingSessionDateIST() when omitted', () => {
    // Smoke-test only: confirms the default-parameter wiring resolves to
    // SOME string without throwing. Call sites in expiryPnl.js/pageLoad.js
    // always pass sessionToday explicitly (see their own comments on why).
    expect(typeof isPastExpiryFreezeWindow('2020-01-01')).toBe('boolean');
    expect(typeof tradingSessionDateIST()).toBe('string');
  });
});

describe('lastWeekdayIsoBefore', () => {
  it('Monday -> previous Friday', () => {
    expect(lastWeekdayIsoBefore('2026-11-30')).toBe('2026-11-27');
  });

  it('Saturday -> previous Friday', () => {
    expect(lastWeekdayIsoBefore('2026-11-28')).toBe('2026-11-27');
  });

  it('a mid-week weekday -> the immediately preceding weekday', () => {
    expect(lastWeekdayIsoBefore('2026-11-25')).toBe('2026-11-24'); // Wed -> Tue
  });
});

describe('isMcxFutureStillCurrent — unchanged behaviour, now a thin wrapper', () => {
  const FRIDAY_EXPIRY = '2026-11-27';

  it('still current through the entire weekend grace window', () => {
    expect(isMcxFutureStillCurrent(FRIDAY_EXPIRY, Date.parse('2026-11-28T08:01:00+05:30'))).toBe(true);
    expect(isMcxFutureStillCurrent(FRIDAY_EXPIRY, Date.parse('2026-11-29T12:00:00+05:30'))).toBe(true);
    expect(isMcxFutureStillCurrent(FRIDAY_EXPIRY, Date.parse('2026-11-30T07:59:00+05:30'))).toBe(true);
  });

  it('rolls at the Monday 08:00 IST boundary', () => {
    expect(isMcxFutureStillCurrent(FRIDAY_EXPIRY, Date.parse('2026-11-30T08:00:00+05:30'))).toBe(false);
  });

  it('is the exact negation of isPastExpiryFreezeWindow for a non-null expiry', () => {
    const nowMs = Date.parse('2026-11-29T15:00:00+05:30');
    const sessionToday = tradingSessionDateIST(nowMs);
    expect(isMcxFutureStillCurrent(FRIDAY_EXPIRY, nowMs)).toBe(
      !isPastExpiryFreezeWindow(FRIDAY_EXPIRY, sessionToday)
    );
  });
});
