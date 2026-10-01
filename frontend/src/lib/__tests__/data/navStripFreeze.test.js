import { describe, it, expect } from 'vitest';
import { isSlotFreshAfterTransition } from '$lib/data/navStripFreeze.js';

/**
 * Reproduces the real-money regression: "NavStrip P∆ shows 0 and never
 * recovers until a full page reload, while a position's Day P&L is
 * genuinely non-zero the whole time." See navStripFreeze.js's file header
 * for the full incident writeup.
 *
 * Old (buggy) gate: `open && _pollCycleStamp <= _openTransitionStamp`,
 * where `_pollCycleStamp` only advances when the SHARED `bookPollerTick`
 * fires. If that shared poller stalls/lags, the gate stays shut forever
 * regardless of how fresh the backing store's own data actually is.
 *
 * New gate (this module): keyed directly off the backing store's own
 * `lastFetch` timestamp — independent of the shared poller's health.
 */
describe('isSlotFreshAfterTransition', () => {
  it('is always fresh when the market is closed (never gated)', () => {
    // Market closed — gate never applies, regardless of fetch timing.
    expect(isSlotFreshAfterTransition(false, 0, 1_000)).toBe(true);
    expect(isSlotFreshAfterTransition(false, 500, 1_000)).toBe(true);
  });

  it('stays frozen immediately after a transition, before any fresh fetch lands', () => {
    const openTransitionAt = 1_000;
    // lastFetch is from BEFORE the transition (stale data from the prior
    // session / pre-mode-switch engine) — must stay frozen.
    expect(isSlotFreshAfterTransition(true, 900, openTransitionAt)).toBe(false);
    // lastFetch exactly at the transition instant — still not "after".
    expect(isSlotFreshAfterTransition(true, 1_000, openTransitionAt)).toBe(false);
  });

  it('releases as soon as a fetch lands strictly after the transition', () => {
    const openTransitionAt = 1_000;
    expect(isSlotFreshAfterTransition(true, 1_001, openTransitionAt)).toBe(true);
    expect(isSlotFreshAfterTransition(true, 50_000, openTransitionAt)).toBe(true);
  });

  it('regression guard: release does NOT depend on a separate tick counter — only on lastFetch vs the transition timestamp', () => {
    // This is the crux of the fix: the OLD implementation compared a
    // shared poll-cycle counter against a snapshot of that same counter.
    // If the shared poller never ticked again (simulating a stalled/
    // hung central poller), the old gate would stay shut forever even
    // though the store's lastFetch legitimately advanced past the
    // transition. The new pure function has no dependency on any such
    // counter at all — only on the store's own lastFetch — so a fresh
    // fetch landing is sufficient to release the gate on its own.
    const openTransitionAt = Date.now();
    const freshFetchLandedLater = openTransitionAt + 5_000; // 5s later, a real poll landed
    expect(isSlotFreshAfterTransition(true, freshFetchLandedLater, openTransitionAt)).toBe(true);
  });

  it('a stale/never-landed fetch (lastFetch=0) stays frozen while open', () => {
    // Cold-start / never-successfully-fetched store — must not be
    // mistaken for "fresh" just because 0 is a number.
    expect(isSlotFreshAfterTransition(true, 0, 0)).toBe(false);
  });
});
