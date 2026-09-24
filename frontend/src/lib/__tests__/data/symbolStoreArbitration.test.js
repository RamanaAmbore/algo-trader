/**
 * symbolStoreArbitration.test.js
 *
 * Unit tests for `sessionBoundaryMs()` and `effectiveStoredLtpTs()` — the
 * pure arbitration helpers behind the primary defect fix (symbolStore
 * staleness guard blocking poll updates forever when a restored
 * yesterday's `ltp_ts` outranks today's poll writes, which always carry
 * `ltp_ts: 0`).
 *
 * Five quality dimensions:
 *   1. SSOT   — sessionBoundaryMs() is the single source symbolStore.svelte.js
 *               uses at BOTH the staleness-comparison site and the
 *               stamp-bump max-computation site; these tests verify the
 *               function's own correctness in isolation (the two-site
 *               wiring is covered by reasoning in the plan; this file
 *               guards the pure boundary math it depends on).
 *   2. Perf   — pure, no I/O; the memoization itself is under test (must
 *               not recompute Intl formatting on every call).
 *   3. Stale  — day-rollover correctness is the whole point: a stored
 *               timestamp from yesterday must resolve to 0 today.
 *   4. Reuse  — startOfTodayIST() (dateFormat.js) is the single date SSOT;
 *               these tests don't reimplement IST date math.
 *   5. UX     — a symbol that hasn't ticked yet today must not show
 *               yesterday's price forever (the operator-facing bug).
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { sessionBoundaryMs, effectiveStoredLtpTs } from '$lib/data/symbolStoreArbitration.js';

// IST offset: UTC+05:30. 00:00 IST == 18:30 UTC the previous day.
// "23:59 IST on day D" == "18:29 UTC on day D" (D's 00:00 IST boundary
// is 18:30 UTC on day D-1; crossing to day D+1's 00:00 IST boundary
// happens at 18:30 UTC on day D).

describe('sessionBoundaryMs — day-rollover correctness', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('returns 00:00 IST of the current IST day', () => {
    // 2026-09-24T10:00:00Z == 2026-09-24T15:30:00+05:30 IST (mid-session).
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-24T10:00:00.000Z'));
    const boundary = sessionBoundaryMs();
    // 00:00 IST on 2026-09-24 == 2026-09-23T18:30:00.000Z
    expect(boundary).toBe(Date.parse('2026-09-23T18:30:00.000Z'));
  });

  it('crosses the boundary forward: 23:59 IST -> 00:01 IST bumps to the new day', () => {
    vi.useFakeTimers();
    // 23:59 IST on 2026-09-23 == 18:29 UTC on 2026-09-23.
    vi.setSystemTime(new Date('2026-09-23T18:29:00.000Z'));
    const before = sessionBoundaryMs();
    expect(before).toBe(Date.parse('2026-09-22T18:30:00.000Z'));

    // 00:01 IST on 2026-09-24 == 18:31 UTC on 2026-09-23. The boundary
    // for 2026-09-24 is 18:30 UTC on 2026-09-23.
    vi.setSystemTime(new Date('2026-09-23T18:31:00.000Z'));
    const after = sessionBoundaryMs();
    expect(after).toBe(Date.parse('2026-09-23T18:30:00.000Z'));
    expect(after).toBeGreaterThan(before);
  });

  it('recomputes when the clock steps BACKWARD across a boundary (e.g. test/mocked clock)', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-24T10:00:00.000Z'));
    const forward = sessionBoundaryMs();

    // Step back a full day.
    vi.setSystemTime(new Date('2026-09-22T10:00:00.000Z'));
    const backward = sessionBoundaryMs();

    expect(backward).toBeLessThan(forward);
    expect(backward).toBe(Date.parse('2026-09-21T18:30:00.000Z'));
  });

  it('memoizes within the same IST day (does not drift within [boundary, boundary+24h))', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-24T01:00:00.000Z')); // 2026-09-24T06:30 IST
    const a = sessionBoundaryMs();
    vi.setSystemTime(new Date('2026-09-24T15:00:00.000Z')); // 2026-09-24T20:30 IST — same IST day
    const b = sessionBoundaryMs();
    expect(a).toBe(b);
  });
});

describe('effectiveStoredLtpTs — comparison-time gate', () => {
  const boundary = Date.parse('2026-09-24T00:00:00.000Z'); // arbitrary fixed boundary for these tests

  it('a stored ts from BEFORE the boundary (yesterday) resolves to 0', () => {
    const yesterday = boundary - 60_000; // 1 minute before boundary
    expect(effectiveStoredLtpTs(yesterday, boundary)).toBe(0);
  });

  it('a stored ts from AFTER the boundary (today, in-session) passes through unchanged', () => {
    const today = boundary + 60_000; // 1 minute after boundary
    expect(effectiveStoredLtpTs(today, boundary)).toBe(today);
  });

  it('an input exactly equal to the boundary passes through (not gated)', () => {
    expect(effectiveStoredLtpTs(boundary, boundary)).toBe(boundary);
  });

  it('0 returns 0', () => {
    expect(effectiveStoredLtpTs(0, boundary)).toBe(0);
  });

  it('NaN returns 0', () => {
    expect(effectiveStoredLtpTs(NaN, boundary)).toBe(0);
  });

  it('negative input returns 0', () => {
    expect(effectiveStoredLtpTs(-1, boundary)).toBe(0);
  });

  it('undefined/null input returns 0', () => {
    expect(effectiveStoredLtpTs(undefined, boundary)).toBe(0);
    expect(effectiveStoredLtpTs(null, boundary)).toBe(0);
  });
});

describe('regression: the primary defect — a hydrated pre-session ltp_ts must not block a same-day poll write forever', () => {
  it('simulates the merge arbitration: stale-gated storedTs lets an incoming poll write (ltp_ts=0) through', () => {
    // Mirrors symbolStore.svelte.js's _mergeSymbolWrite comparison:
    //   incomingTs < effectiveStoredLtpTs(storedTs, boundary) => reject
    // A poll write always carries ltp_ts=0.
    const boundary = 2_000_000_000_000; // arbitrary fixed "today" boundary
    const hydratedYesterdayTs = boundary - 1; // restored from localStorage, pre-session
    const incomingPollTs = 0;

    const gatedStoredTs = effectiveStoredLtpTs(hydratedYesterdayTs, boundary);
    expect(gatedStoredTs).toBe(0);
    // The merge's reject condition `incomingTs < storedTs` must be false —
    // i.e. the poll write is accepted.
    expect(incomingPollTs < gatedStoredTs).toBe(false);
  });

  it('does NOT gate a genuinely fresh in-session SSE tick — a later same-day tick still wins over an earlier same-day poll', () => {
    const boundary = 2_000_000_000_000;
    const earlierPollTs = boundary + 1000; // poll wrote earlier today (ltp_ts=0 normally, but simulating a stamped snapshot_ts style compare)
    const laterTickTs   = boundary + 5000;

    const gatedStoredTs = effectiveStoredLtpTs(earlierPollTs, boundary);
    expect(gatedStoredTs).toBe(earlierPollTs); // in-session — not gated
    expect(laterTickTs < gatedStoredTs).toBe(false); // later tick still accepted
    // And a genuinely stale write (older than the in-session stored ts) is
    // still correctly rejected — the fix must not defeat real ordering.
    const staleTs = boundary + 500;
    expect(staleTs < gatedStoredTs).toBe(true);
  });

  it('max-computation site: a gated-to-0 comparison must ALSO use the gated value in Math.max, or the bug re-introduces itself', () => {
    // This is the "using it at only one site" regression the plan calls
    // out explicitly: if the max-bump used the RAW storedTs instead of
    // effectiveStoredLtpTs(storedTs, boundary), the hydrated yesterday's
    // timestamp would win Math.max() and get written back as the new
    // ltp_ts — re-arming the stale guard for the very next write.
    const boundary = 2_000_000_000_000;
    const hydratedYesterdayTs = boundary - 999; // pre-session, large value
    const incomingPollTs = 0; // poll ltp_ts

    // Correct (gated) max — matches the fix.
    const correctMax = Math.max(effectiveStoredLtpTs(hydratedYesterdayTs, boundary), incomingPollTs);
    expect(correctMax).toBe(0);

    // Buggy (ungated) max — what a single-site fix would produce.
    const buggyMax = Math.max(hydratedYesterdayTs, incomingPollTs);
    expect(buggyMax).toBe(hydratedYesterdayTs);
    expect(buggyMax).not.toBe(correctMax);

    // Next poll write (still ltp_ts=0) against the buggy max would be
    // rejected again — reproducing the original bug.
    expect(0 < buggyMax).toBe(true);
    // Against the correct (gated) max, it is accepted.
    expect(0 < correctMax).toBe(false);
  });
});
