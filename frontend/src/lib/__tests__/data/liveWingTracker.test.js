/**
 * liveWingTracker.test.js — regression test for the 2026-11-03 live
 * incident (Chain tab hangs the instant it is pressed, modal close
 * button unresponsive).
 *
 * Root cause, confirmed by reading the real code: `refreshLiveWingState()`
 * in `liveWingTracker.svelte.js` used to write `_tracked = trackedParents`
 * UNCONDITIONALLY on every call. `computeLiveWingState()` (liveWingBadge.js)
 * always builds `nextTracked` as a brand-new array — `const nextTracked =
 * []`, pushed into — even when the input is empty and nothing changed. A
 * brand-new array is never `===` the old one, so the `$effect` in
 * `OptionChainTab.svelte` that reads this state (via this function) and
 * writes it back in the SAME tick saw `_tracked` as "changed" on every
 * single call — including the baseline case where the operator had never
 * placed a live Bracket order from Chain that session (`_tracked` starts
 * as `[]`; `[] !== []` by reference). That is a synchronous, unconditional
 * infinite effect loop starting the instant the Chain tab mounts.
 *
 * `liveWingTracker.svelte.js` uses Svelte 5 `$state` runes at module
 * scope. Per this project's established convention (see
 * `tickFlash.test.js`'s own header comment), the vitest environment
 * (`environment: 'node'`, no sveltekit() Vite plugin) cannot compile rune
 * syntax for a direct unit-test import — so this test extracts and
 * exercises the REAL `_trackedListsEqual()` function body from the real
 * source file (not a hand-written mirror), which is the exact mechanism
 * that now prevents the unconditional write.
 *
 * Five quality dimensions:
 *   1. SSOT   — tests the real extracted `_trackedListsEqual` source, not
 *               a re-typed copy that could silently drift from it.
 *   2. Perf   — this IS the performance/infinite-loop fix under test.
 *   3. Stale  — confirms the old unconditional-write pattern is gone.
 *   4. Reuse  — no new comparison helper invented for this test.
 *   5. UX     — the empty/baseline case (never placed a live Bracket
 *               order this session) is the exact case that hung the tab;
 *               it is tested explicitly, not just the non-empty case.
 */
import { describe, it, expect, beforeAll } from 'vitest';
import trackerSrc from '$lib/data/liveWingTracker.svelte.js?raw';

/** Extracted from the real source — see the `beforeAll` below. */
let trackedListsEqual;

beforeAll(() => {
  const start = trackerSrc.indexOf('function _trackedListsEqual(');
  expect(start, '_trackedListsEqual not found in liveWingTracker.svelte.js').toBeGreaterThan(-1);
  const end = trackerSrc.indexOf('\n}', start) + 2;
  const fnSrc = trackerSrc.slice(start, end);
  // eslint-disable-next-line no-new-func
  trackedListsEqual = new Function(`${fnSrc}\nreturn _trackedListsEqual;`)();
});

describe('liveWingTracker.svelte.js — _trackedListsEqual (the infinite-loop fix)', () => {
  it('two empty arrays are equal — THE case that caused the live incident (never placed a live Bracket order this session)', () => {
    expect(trackedListsEqual([], [])).toBe(true);
  });

  it('two non-empty lists with identical content are equal despite being different array/object references', () => {
    const a = [{ brokerOrderId: '123', lastSeenAt: 1000 }];
    const b = [{ brokerOrderId: '123', lastSeenAt: 1000 }]; // fresh objects, same content
    expect(a).not.toBe(b);
    expect(a[0]).not.toBe(b[0]);
    expect(trackedListsEqual(a, b)).toBe(true);
  });

  it('a different length is never equal', () => {
    expect(trackedListsEqual([], [{ brokerOrderId: '1', lastSeenAt: 1 }])).toBe(false);
  });

  it('a changed brokerOrderId (a different parent tracked) is not equal', () => {
    const a = [{ brokerOrderId: '123', lastSeenAt: 1000 }];
    const b = [{ brokerOrderId: '456', lastSeenAt: 1000 }];
    expect(trackedListsEqual(a, b)).toBe(false);
  });

  it('a changed lastSeenAt (real activity — parent/wing resolved this tick) is not equal', () => {
    const a = [{ brokerOrderId: '123', lastSeenAt: 1000 }];
    const b = [{ brokerOrderId: '123', lastSeenAt: 2000 }];
    expect(trackedListsEqual(a, b)).toBe(false);
  });

  it('the real refreshLiveWingState() source guards its write with this function, never writing unconditionally', () => {
    const idx = trackerSrc.indexOf('function refreshLiveWingState(');
    expect(idx).toBeGreaterThan(-1);
    const body = trackerSrc.slice(idx);
    // The fix: the write only happens inside this guard — never a bare,
    // unconditional `_tracked = trackedParents;` sitting directly in the
    // function body (the pre-incident shape).
    expect(body).toMatch(/if\s*\(!_trackedListsEqual\(_tracked,\s*trackedParents\)\)\s*\{\s*\n\s*_tracked = trackedParents;/);
    const guardIdx = body.indexOf('if (!_trackedListsEqual(');
    const beforeGuard = body.slice(0, guardIdx);
    // No unconditional write anywhere before the guard.
    expect(beforeGuard).not.toMatch(/^\s{2}_tracked = trackedParents;/m);
  });
});
