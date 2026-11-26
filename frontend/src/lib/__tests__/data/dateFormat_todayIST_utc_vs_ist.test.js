/**
 * dateFormat_todayIST_utc_vs_ist.test.js — Vitest regression for the DTE
 * timezone bug fixed in OptionsPayoff.svelte's `anchorDaysToExpiry`
 * (2026-11-25).
 *
 * `anchorDaysToExpiry` used to compute "today" via a bare
 * `new Date().toISOString().slice(0, 10)` — the browser/viewer's own UTC
 * calendar date, not IST. The fix replaces it with `todayIST()`
 * ($lib/dateFormat.js — this app's canonical calendar-day boundary).
 *
 * This file can't import OptionsPayoff.svelte directly: this project's
 * vitest.config.js runs in a plain `node` environment with no Svelte
 * plugin wired (confirmed — `environment: 'node'`, no
 * `@sveltejs/vite-plugin-svelte` in `test.include`/`plugins`), so a
 * `.svelte` file can't be imported or mounted here (matches CLAUDE.md's
 * own test-location map: `*.svelte` → Playwright, not Vitest). Per
 * CLAUDE.md's SSOT rule, this test also deliberately does NOT re-type a
 * hand-mirrored copy of the old/new date-math formula — it exercises the
 * REAL `todayIST()` the fixed component now imports, at the exact
 * instant that discriminates the bug from the fix. The static source
 * guard in `e2e/derivatives_payoff_dart_number_parity.spec.js`
 * ("SSOT: real Day P&L dart ... and IST-anchored anchorDaysToExpiry")
 * confirms the component actually calls this real function, so the two
 * tests together cover both "the right helper exists and behaves
 * correctly" (here) and "the component actually uses it" (there).
 *
 * Discriminating instant: UTC and IST calendar dates only disagree
 * between 00:00 and 05:29 IST (IST = UTC+5:30), when IST has already
 * rolled to a new date but UTC has not yet. 02:00 IST on 2026-11-25 is
 * 20:30 UTC on 2026-11-24 — the OLD bare
 * `new Date().toISOString().slice(0, 10)` reads "2026-11-24" (one day
 * behind), while the real IST calendar date is "2026-11-25".
 *
 * The brief's own example instant (22:00 IST) does NOT discriminate —
 * at 22:00 IST it is 16:30 UTC the SAME date, so old and new code would
 * agree and a test anchored there would pass against the unfixed
 * component too. This file deliberately uses 02:00 IST instead; see the
 * explicit before/after assertions below, which prove the chosen instant
 * really does disagree.
 *
 * Five quality dimensions:
 *  1. SSOT   — exercises the real `todayIST()` (`$lib/dateFormat.js`),
 *              the exact function `anchorDaysToExpiry` now imports and
 *              calls — not a reimplementation of either formula.
 *  2. Perf   — pure unit, fake timers, no I/O.
 *  3. Stale  — targets the one 5.5-hour window (00:00-05:29 IST) where
 *              the old UTC-anchored bug actually manifested; a contrast
 *              check elsewhere in the day proves old/new agree outside it.
 *  4. Reuse  — same `vi.useFakeTimers()` + explicit `+05:30` offset
 *              pattern as `dateFormat_isCurrentTradingSession.test.js`.
 *  5. UX     — this is the exact "today" anchor the DTE chip's "rolls in
 *              N days" copy is computed from; getting it wrong shows an
 *              off-by-one DTE for the whole early-morning IST window.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { todayIST } from '$lib/dateFormat.js';

/** Set the fake system clock to a specific IST wall-clock instant. */
function setISTTime(/** @type {string} */ isoDateNoOffset) {
  vi.setSystemTime(new Date(`${isoDateNoOffset}+05:30`));
}

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); });

describe('todayIST() vs UTC-anchored "today" — the DTE-anchor discriminating instant', () => {
  it('at 02:00 IST (20:30 UTC the PREVIOUS day), todayIST() returns the correct IST date, not the old UTC-anchored one', () => {
    setISTTime('2026-11-25T02:00:00');

    // The real IST calendar date — what anchorDaysToExpiry's fixed code
    // now anchors "today" to.
    expect(todayIST()).toBe('2026-11-25');

    // Documents the bug the fix removes: the OLD expression
    // (`new Date().toISOString().slice(0, 10)`) reads the UTC date,
    // which at this exact instant is ONE DAY BEHIND the real IST date —
    // this is the literal expression `anchorDaysToExpiry` used to use.
    expect(new Date().toISOString().slice(0, 10)).toBe('2026-11-24');

    // The two must genuinely disagree at this instant — otherwise this
    // test would pass against the unfixed code too, proving nothing.
    expect(todayIST()).not.toBe(new Date().toISOString().slice(0, 10));
  });

  it('contrast: at 22:00 IST (same UTC calendar date), UTC-anchored "today" and todayIST() agree — this is why that instant does NOT discriminate the bug', () => {
    setISTTime('2026-11-24T22:00:00');

    expect(todayIST()).toBe('2026-11-24');
    expect(new Date().toISOString().slice(0, 10)).toBe('2026-11-24');
    expect(todayIST()).toBe(new Date().toISOString().slice(0, 10));
  });

  it('just after the IST/UTC disagreement window closes (05:30 IST), both read the new IST date again', () => {
    setISTTime('2026-11-25T05:30:00');

    expect(todayIST()).toBe('2026-11-25');
    // 05:30 IST = 00:00 UTC same day — UTC has now also rolled over.
    expect(new Date().toISOString().slice(0, 10)).toBe('2026-11-25');
  });

  it('one minute before the window closes (05:29 IST), UTC-anchored "today" is still one day behind', () => {
    setISTTime('2026-11-25T05:29:00');

    expect(todayIST()).toBe('2026-11-25');
    expect(new Date().toISOString().slice(0, 10)).toBe('2026-11-24');
  });
});
