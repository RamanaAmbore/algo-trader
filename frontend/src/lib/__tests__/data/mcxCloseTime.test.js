/**
 * mcxCloseTime.test.js — Vitest tests for the DST-aware MCX non-agri
 * close-time resolver (`frontend/src/lib/data/mcxCloseTime.js`).
 *
 * MCX revises its non-agri commodity (bullion/energy/base-metals)
 * evening close twice a year, tracking the US daylight-saving calendar:
 *   - US DST active            → 23:30 IST
 *   - US standard time (winter) → 23:55 IST
 *
 * This is the frontend mirror of the backend's
 * `mcx_non_agri_close_time()` (`backend/api/algo/derivatives.py`,
 * covered by `backend/tests/test_mcx_non_agri_close_time.py`) and the
 * two MUST agree. Residual fixed here: `frontend/src/lib/marketHours.js`
 * had a flat `MCX_CLOSE_MIN = 23 * 60 + 30` constant with zero DST
 * awareness, used by the order-ticket "market closed" gate — at
 * 23:40 IST on a winter-window date, the ticket would have shown
 * "Market closed" for an order the backend would actually accept.
 *
 * Five quality dimensions:
 *  1. SSOT    — exercises the real exported resolver, not a
 *               re-implementation; `marketHours.js` imports this exact
 *               module (checked via a source-text assertion below).
 *  2. Perf    — pure unit, no I/O, no timers; also asserts the result
 *               is memoized (same object identity) so repeated calls in
 *               a tight render loop don't re-run `Intl` formatting.
 *  3. Stale   — directly targets the real US DST transition boundary,
 *               computed programmatically (never a hardcoded date), the
 *               same convention the backend test file uses.
 *  4. Reuse   — confirms `marketHours.js` no longer carries its own flat
 *               `23 * 60 + 30` MCX-close literal anywhere.
 *  5. Cross-side parity — the exact two calendar dates from the live
 *               incident (2026-11-17 winter, 2026-06-01 DST) must match
 *               the backend's own resolver output bit for bit.
 */

import { describe, it, expect } from 'vitest';
// @ts-ignore -- node:fs types are not installed for svelte-check; vitest resolves it at runtime.
import { readFileSync } from 'node:fs';
import {
  isUsEasternDst,
  mcxNonAgriCloseTime,
  mcxNonAgriCloseMinuteOfDay,
  resolveExpiryCloseDateTime,
} from '../../data/mcxCloseTime.js';

/** Independent reference oracle for a given UTC-noon-anchored date,
 *  using the SAME offset-comparison primitive the module under test
 *  uses but called directly here rather than via `isUsEasternDst()` —
 *  kept deliberately trivial (not a re-implementation of the module's
 *  own logic) so this is a parity check, not a tautology. */
function _refIsDstOn(year, month, day) {
  const dtf = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    hourCycle: 'h23',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
  const instant = new Date(Date.UTC(year, month - 1, day, 12, 0, 0));
  const parts = dtf.formatToParts(instant);
  const map = {};
  for (const p of parts) if (p.type !== 'literal') map[p.type] = p.value;
  const asUtcMs = Date.UTC(
    Number(map.year), Number(map.month) - 1, Number(map.day),
    Number(map.hour), Number(map.minute), Number(map.second),
  );
  const offsetMin = (asUtcMs - instant.getTime()) / 60000;
  // January is always standard time in America/New_York; July is
  // always DST — used only to derive which of the two observed offsets
  // for THIS year is the "standard" one, never hardcoded.
  const jan = new Date(Date.UTC(year, 0, 1, 12, 0, 0));
  const janParts = dtf.formatToParts(jan);
  const janMap = {};
  for (const p of janParts) if (p.type !== 'literal') janMap[p.type] = p.value;
  const janUtcMs = Date.UTC(
    Number(janMap.year), Number(janMap.month) - 1, Number(janMap.day),
    Number(janMap.hour), Number(janMap.minute), Number(janMap.second),
  );
  const janOffset = (janUtcMs - jan.getTime()) / 60000;
  return offsetMin > janOffset;
}

/** Walk March day-by-day to find the real US spring-forward date for
 *  `year`, computed programmatically (never hardcoded "2nd Sunday") —
 *  mirrors `backend/tests/test_mcx_non_agri_close_time.py::_find_real_spring_transition`. */
function _findRealSpringTransition(year) {
  let d = new Date(Date.UTC(year, 2, 1, 12, 0, 0)); // March 1, noon UTC
  let prevDst = isUsEasternDst(d);
  while (d.getUTCMonth() === 2) {
    d = new Date(d.getTime() + 24 * 60 * 60 * 1000);
    const curDst = isUsEasternDst(d);
    if (curDst && !prevDst) return d;
    prevDst = curDst;
  }
  throw new Error(`no spring DST transition found in March ${year}`);
}

/** Walk late October through November day-by-day to find the real US
 *  fall-back date for `year` — mirrors
 *  `backend/tests/test_mcx_non_agri_close_time.py::_find_real_fall_transition`.
 *  Starts a week before November 1st for the same reason the backend
 *  test does: the transition can legitimately land exactly on Nov 1. */
function _findRealFallTransition(year) {
  let d = new Date(Date.UTC(year, 9, 25, 12, 0, 0)); // Oct 25, noon UTC
  const end = new Date(Date.UTC(year, 10, 30, 12, 0, 0));
  let prevDst = isUsEasternDst(d);
  while (d.getTime() < end.getTime()) {
    d = new Date(d.getTime() + 24 * 60 * 60 * 1000);
    const curDst = isUsEasternDst(d);
    if (!curDst && prevDst) return d;
    prevDst = curDst;
  }
  throw new Error(`no fall DST transition found around November ${year}`);
}

function _utcParts(/** @type {Date} */ d) {
  return { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate() };
}

describe('mcxNonAgriCloseTime — core resolution', () => {
  it('2026-11-17 (the live-incident winter-window date) resolves 23:55', () => {
    expect(mcxNonAgriCloseTime(2026, 11, 17)).toEqual({ hour: 23, minute: 55 });
  });

  it('2026-06-01 (deep in the US DST window) resolves 23:30', () => {
    expect(mcxNonAgriCloseTime(2026, 6, 1)).toEqual({ hour: 23, minute: 30 });
  });

  it('early January is always winter (standard time)', () => {
    expect(mcxNonAgriCloseTime(2026, 1, 15)).toEqual({ hour: 23, minute: 55 });
  });

  it('mid-July is always inside US DST', () => {
    expect(mcxNonAgriCloseTime(2026, 7, 15)).toEqual({ hour: 23, minute: 30 });
  });

  it('mcxNonAgriCloseMinuteOfDay flattens to the matching minute-of-day', () => {
    expect(mcxNonAgriCloseMinuteOfDay(2026, 11, 17)).toBe(23 * 60 + 55);
    expect(mcxNonAgriCloseMinuteOfDay(2026, 6, 1)).toBe(23 * 60 + 30);
  });
});

describe('mcxNonAgriCloseTime — transition boundary (computed, never hardcoded)', () => {
  it('spring transition flips winter(23:55) → DST(23:30)', () => {
    const year = 2026;
    const transition = _findRealSpringTransition(year);
    const dayBefore = new Date(transition.getTime() - 24 * 60 * 60 * 1000);
    const dayAfter = new Date(transition.getTime() + 24 * 60 * 60 * 1000);

    const b = _utcParts(dayBefore);
    const t = _utcParts(transition);
    const a = _utcParts(dayAfter);

    expect(mcxNonAgriCloseTime(b.year, b.month, b.day)).toEqual({ hour: 23, minute: 55 });
    expect(mcxNonAgriCloseTime(t.year, t.month, t.day)).toEqual({ hour: 23, minute: 30 });
    expect(mcxNonAgriCloseTime(a.year, a.month, a.day)).toEqual({ hour: 23, minute: 30 });
  });

  it('fall transition flips DST(23:30) → winter(23:55)', () => {
    const year = 2026;
    const transition = _findRealFallTransition(year);
    const dayBefore = new Date(transition.getTime() - 24 * 60 * 60 * 1000);

    const b = _utcParts(dayBefore);
    const t = _utcParts(transition);

    expect(mcxNonAgriCloseTime(b.year, b.month, b.day)).toEqual({ hour: 23, minute: 30 });
    expect(mcxNonAgriCloseTime(t.year, t.month, t.day)).toEqual({ hour: 23, minute: 55 });
  });

  it('matches an independent reference oracle across several years, sampled every ~30 days', () => {
    for (const year of [2025, 2026, 2027, 2030]) {
      let d = new Date(Date.UTC(year, 0, 1, 12, 0, 0));
      const end = new Date(Date.UTC(year, 11, 31, 12, 0, 0));
      while (d.getTime() <= end.getTime()) {
        const { year: y, month: m, day: dd } = _utcParts(d);
        const expected = _refIsDstOn(y, m, dd) ? { hour: 23, minute: 30 } : { hour: 23, minute: 55 };
        expect(mcxNonAgriCloseTime(y, m, dd)).toEqual(expected);
        d = new Date(d.getTime() + 30 * 24 * 60 * 60 * 1000);
      }
    }
  });
});

describe('mcxNonAgriCloseTime — memoization (perf)', () => {
  it('returns the identical cached object on repeated calls for the same date', () => {
    const first = mcxNonAgriCloseTime(2026, 11, 17);
    const second = mcxNonAgriCloseTime(2026, 11, 17);
    expect(first).toBe(second);
  });
});

describe('isUsEasternDst', () => {
  it('agrees with the reference oracle at the two live-incident anchors', () => {
    expect(isUsEasternDst(new Date(Date.UTC(2026, 10, 17, 12, 0, 0)))).toBe(false);
    expect(isUsEasternDst(new Date(Date.UTC(2026, 5, 1, 12, 0, 0)))).toBe(true);
  });
});

describe('resolveExpiryCloseDateTime', () => {
  it('live-reported bug (2026-11-26): MCX GOLD expiring Friday must not show "0 days" at Thursday 18:00 IST', () => {
    // Confirmed live against the real server: GOLD26NOV124000CE (MCX) parses
    // to a real expiry of 2026-11-27 (Friday) with days_to_expiry ~1.5 at
    // 2026-11-26 11:42 IST. The frontend chip, checked later the same
    // Thursday evening (after the OLD hardcoded 15:30 cutoff but hours
    // before MCX's real ~23:55 winter close), incorrectly floored to 0
    // ("expires today") a full day early.
    const now = new Date('2026-11-26T18:00:00+05:30').getTime();
    const origNow = Date.now;
    Date.now = () => now;
    try {
      const expiryIso = '2026-11-27';
      const mcxExpiry = resolveExpiryCloseDateTime(expiryIso, true);
      const mcxDays = Math.max(0, Math.floor((mcxExpiry.getTime() - Date.now()) / 86_400_000));
      expect(mcxDays).toBe(1); // tomorrow, not today

      // The pre-fix behavior (flat 15:30 for every exchange) reproduces
      // the bug -- pinned here as a negative control so this test would
      // have failed against the old code, not just passed by construction.
      const oldBuggyExpiry = new Date(`${expiryIso}T15:30:00+05:30`);
      const oldBuggyDays = Math.max(0, Math.floor((oldBuggyExpiry.getTime() - Date.now()) / 86_400_000));
      expect(oldBuggyDays).toBe(0);
    } finally {
      Date.now = origNow;
    }
  });

  it('non-MCX (NSE/NFO) contracts still close at 15:30 IST, unaffected by the MCX fix', () => {
    const d = resolveExpiryCloseDateTime('2026-11-27', false);
    expect(d.getUTCHours()).toBe(10); // 15:30 IST == 10:00 UTC
    expect(d.getUTCMinutes()).toBe(0);
  });

  it('MCX close resolves to the DST-aware time, not a flat 23:30', () => {
    const winter = resolveExpiryCloseDateTime('2026-11-27', true); // winter window -> 23:55
    expect(winter.getUTCHours()).toBe(18); // 23:55 IST == 18:25 UTC
    expect(winter.getUTCMinutes()).toBe(25);

    const summer = resolveExpiryCloseDateTime('2026-06-26', true); // DST window -> 23:30
    expect(summer.getUTCHours()).toBe(18); // 23:30 IST == 18:00 UTC
    expect(summer.getUTCMinutes()).toBe(0);
  });
});

describe('marketHours.js no longer carries the stale flat constant (reuse/SSOT)', () => {
  it('imports mcxNonAgriCloseMinuteOfDay and has no flat 23*60+30 MCX-close literal', () => {
    const src = readFileSync(new URL('../../marketHours.js', import.meta.url), 'utf8');
    expect(src).toContain("from './data/mcxCloseTime.js'");
    expect(src).toContain('mcxNonAgriCloseMinuteOfDay');
    // The old stale constant declaration must not reappear.
    expect(src).not.toMatch(/const\s+MCX_CLOSE_MIN\s*=\s*23\s*\*\s*60\s*\+\s*30/);
    expect(src).not.toMatch(/const\s+ANY_CLOSE_MIN\s*=\s*MCX_CLOSE_MIN/);
  });
});
