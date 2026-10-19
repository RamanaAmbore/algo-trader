/**
 * tickFlash.test.js
 *
 * Unit tests for the createTickFlash hidden-tab guard.
 *
 * Why a local helper (not a direct import):
 *   tickFlash.svelte.js uses Svelte 5 $state runes at module scope.
 *   The vitest environment uses `environment: 'node'` without the
 *   sveltekit() Vite plugin, so rune syntax does not compile there.
 *   Following the same pattern as positionsDayPnlDualStore.test.js,
 *   we test the pure change-detection logic using a local helper that
 *   mirrors the createTickFlash update/classOf contract exactly.
 *
 * The bug fixed: the hidden-tab guard was placed AFTER `prev[key] = v`,
 * so hidden polls silently advanced the baseline. On tab-return, the
 * first visible poll compared new value against the hidden-period final
 * value → no change detected → no flash. Fix: guard fires before any
 * prev[key] read or write.
 *
 * Five quality dimensions:
 *   1. SSOT   — tests the canonical update/classOf contract
 *   2. Perf   — pure unit, no DOM / network / Svelte runtime
 *   3. Stale  — hidden polls must not advance prev baseline
 *   4. Reuse  — local helper models the real code path without rune env
 *   5. UX     — tab-return flash fires correctly for derivatives + Pulse
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import tickFlashSrc from '$lib/data/tickFlash.svelte.js?raw';

// ── Visibility state mock ─────────────────────────────────────────────────────
//
// Tests run in node environment (no DOM). We mock a minimal document object
// with a configurable visibilityState so the guard in the local helper
// (`typeof document !== 'undefined' && document.visibilityState === 'hidden'`)
// can be toggled per test. The mock is installed on globalThis before each
// test and removed after.

/** @type {'visible' | 'hidden'} */
let _mockVisibility = 'visible';

function setVisible() { _mockVisibility = 'visible'; }
function setHidden()  { _mockVisibility = 'hidden'; }

beforeEach(() => {
  vi.useFakeTimers();
  _mockVisibility = 'visible';
  // @ts-ignore — minimal document mock for visibility guard; node env has no DOM
  globalThis.document = { get visibilityState() { return _mockVisibility; } };
});

afterEach(() => {
  _mockVisibility = 'visible';
  // @ts-ignore
  delete globalThis.document;
  vi.useRealTimers();
});

// ── Local helper: mirrors the createTickFlash update/classOf contract ─────────
//
// Reproduces the fixed logic: hidden-tab guard fires before prev[key] is
// read or written. The real module uses $state for `classes`; here we use
// a plain object since we only need classOf(), not reactive UI.

function makeTickFlash({ threshold = 0, pctThreshold = 0, durationMs = 300 } = {}) {
  const prev = {};
  const timers = {};
  const classes = {};
  let _pctThreshold = pctThreshold;

  function update(key, value) {
    if (value == null) return;
    const v = Number(value);
    if (!isFinite(v)) return;
    // Guard fires BEFORE prev[key] read/write — mirrors the fixed code.
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    const last = prev[key];
    // First sample establishes the baseline — the ONE unconditional write
    // to prev[key]. Every other write happens only right before a flash
    // actually fires (accumulate-until-threshold fix, 2026-10-19).
    if (last == null) { prev[key] = v; return; }
    if (v === last) return;
    if (Math.abs(v - last) < threshold) return;
    // Item 10 fix (2026-10): compare Math.abs(last), not bare `last` — a
    // negative previous value used to skip the percentage-threshold check
    // entirely (bug: `_pctThreshold > 0 && last > 0`), flashing on every
    // change regardless of threshold.
    if (_pctThreshold > 0 && Math.abs(last) > 0) {
      const changePct = Math.abs((v - last) / last * 100);
      // Accumulate-until-threshold fix: on a sub-threshold move, leave
      // prev[key] UNCHANGED so the next call still compares against this
      // same fixed baseline — small same-direction moves accumulate until
      // the CUMULATIVE change crosses pctThreshold.
      if (changePct < _pctThreshold) return;
    }
    const dir = v > last ? 'up' : 'down';
    // Baseline resets to the value that triggered this flash.
    prev[key] = v;
    classes[key] = dir;
    if (timers[key]) clearTimeout(timers[key]);
    timers[key] = setTimeout(() => {
      classes[key] = '';
      delete timers[key];
    }, durationMs);
  }

  function setPctThreshold(v) { _pctThreshold = v; }

  function classOf(key) {
    const c = classes[key];
    return c === 'up' ? 'tf-up' : c === 'down' ? 'tf-down' : '';
  }

  function dispose() {
    for (const t of Object.values(timers)) clearTimeout(t);
  }

  return { update, classOf, dispose, setPctThreshold };
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('createTickFlash — hidden tab guard fires before prev[key] write', () => {
  it('does not advance prev while hidden; first visible update seeds, second flashes', () => {
    // Simulate hidden tab from the start.
    setHidden();

    const flash = makeTickFlash({ threshold: 0, durationMs: 300 });
    flash.update('x', 100); // hidden — skipped entirely, prev['x'] stays undefined
    flash.update('x', 200); // hidden — also skipped

    // Return to visible.
    setVisible();

    // First visible call: last = undefined → seeds prev['x'] = 200, no flash.
    flash.update('x', 200);
    expect(flash.classOf('x')).toBe('');

    // Second visible call: prev['x'] = 200; 250 > 200 → flash 'up'.
    flash.update('x', 250);
    expect(flash.classOf('x')).toBe('tf-up');

    flash.dispose();
  });

  it('prev is not advanced by hidden polls — change detected on tab-return', () => {
    // Seed with visible tab so prev['x'] = 100 is established.
    const flash = makeTickFlash({ threshold: 0, durationMs: 300 });
    flash.update('x', 100); // seeds prev['x'] = 100, no flash (first call)
    expect(flash.classOf('x')).toBe('');

    // Hide tab, simulate polls advancing the value.
    setHidden();
    flash.update('x', 150); // skipped — prev['x'] stays 100
    flash.update('x', 200); // skipped — prev['x'] stays 100

    // Return to visible.
    setVisible();

    // First visible update: prev['x'] is still 100; 200 > 100 → flash 'up'.
    flash.update('x', 200);
    expect(flash.classOf('x')).toBe('tf-up');

    flash.dispose();
  });

  it('regression: old bug would NOT flash on tab-return (prev advanced silently)', () => {
    // Demonstrates the pre-fix behaviour for contrast:
    // If prev[key] were updated while hidden (old code), the tab-return call
    // of flash.update('x', 200) would compare 200 === 200 → no flash.
    // With the fix, prev['x'] stays at 100 while hidden, so 200 > 100 → flash.
    const flash = makeTickFlash({ threshold: 0, durationMs: 300 });
    flash.update('x', 100); // visible — seeds prev['x'] = 100

    setHidden();
    flash.update('x', 200); // hidden — skipped; prev['x'] stays 100

    setVisible();
    flash.update('x', 200); // prev['x'] = 100; 200 !== 100 → flash 'up'
    expect(flash.classOf('x')).toBe('tf-up'); // must flash (would be '' with old bug)

    flash.dispose();
  });

  it('flash clears after durationMs even on tab-return', () => {
    const flash = makeTickFlash({ threshold: 0, durationMs: 300 });
    flash.update('x', 100); // seed
    flash.update('x', 200); // flash 'up'
    expect(flash.classOf('x')).toBe('tf-up');

    vi.advanceTimersByTime(300);
    expect(flash.classOf('x')).toBe('');

    flash.dispose();
  });

  it('down flash fires when value drops on tab-return', () => {
    const flash = makeTickFlash({ threshold: 0, durationMs: 300 });
    flash.update('x', 200); // seed

    setHidden();
    flash.update('x', 300); // hidden — skipped

    setVisible();
    flash.update('x', 150); // prev['x'] = 200; 150 < 200 → flash 'down'
    expect(flash.classOf('x')).toBe('tf-down');

    flash.dispose();
  });

  it('no flash when value is unchanged after tab-return', () => {
    const flash = makeTickFlash({ threshold: 0, durationMs: 300 });
    flash.update('x', 100); // seed

    setHidden();
    flash.update('x', 150); // hidden — skipped

    setVisible();
    // On return, same value as the last VISIBLE value → no flash.
    flash.update('x', 100); // prev['x'] = 100; same value → no flash
    expect(flash.classOf('x')).toBe('');

    flash.dispose();
  });
});

// ── Item 10 (2026-10) — pctThreshold guard must apply to negative `last` too ──
//
// Bug: `if (_pctThreshold > 0 && last > 0)` — a NEGATIVE previous value
// skipped the percentage-threshold check entirely (the `last > 0` guard
// failed), flashing on every change regardless of the configured
// threshold. Fix: compare `Math.abs(last) > 0`. Affects Legs' Chg% (post
// item-4 fix, which now reuses the LTP key but the same createTickFlash
// instance governs any negative-valued key) and PositionStrip's P&L cells
// when negative.

describe('createTickFlash pctThreshold — Math.abs(last) fix (item 10)', () => {
  it('positive last: sub-threshold change is suppressed (baseline, unaffected by the fix)', () => {
    const flash = makeTickFlash({ threshold: 0, pctThreshold: 5, durationMs: 300 });
    flash.update('x', 100); // seed, last = 100 (positive)
    flash.update('x', 102); // 2% change < 5% threshold → suppressed
    expect(flash.classOf('x')).toBe('');
    flash.dispose();
  });

  it('positive last: above-threshold change still flashes (baseline, unaffected)', () => {
    const flash = makeTickFlash({ threshold: 0, pctThreshold: 5, durationMs: 300 });
    flash.update('x', 100);
    flash.update('x', 110); // 10% change ≥ 5% → flashes
    expect(flash.classOf('x')).toBe('tf-up');
    flash.dispose();
  });

  it('negative last: sub-threshold change is now suppressed (pre-fix: flashed on every change)', () => {
    const flash = makeTickFlash({ threshold: 0, pctThreshold: 5, durationMs: 300 });
    flash.update('x', -100); // seed, last = -100 (negative)
    // 2% move relative to |last| — must be suppressed just like the
    // positive-last case above. Pre-fix, `last > 0` was false for -100,
    // so the whole pctThreshold block was skipped and this flashed.
    flash.update('x', -102);
    expect(flash.classOf('x')).toBe('');
    flash.dispose();
  });

  it('negative last: above-threshold change still flashes correctly (direction = down)', () => {
    const flash = makeTickFlash({ threshold: 0, pctThreshold: 5, durationMs: 300 });
    flash.update('x', -100);
    flash.update('x', -120); // 20% move ≥ 5% → flashes; -120 < -100 → 'down'
    expect(flash.classOf('x')).toBe('tf-down');
    flash.dispose();
  });

  it('negative last moving toward zero (improving): above-threshold flashes up', () => {
    const flash = makeTickFlash({ threshold: 0, pctThreshold: 5, durationMs: 300 });
    flash.update('x', -100);
    flash.update('x', -50); // 50% move ≥ 5% → flashes; -50 > -100 → 'up'
    expect(flash.classOf('x')).toBe('tf-up');
    flash.dispose();
  });

  it('setPctThreshold updates the threshold live, including for negative last values', () => {
    const flash = makeTickFlash({ threshold: 0, pctThreshold: 0, durationMs: 300 });
    flash.update('x', -100);
    flash.setPctThreshold(10);
    flash.update('x', -103); // 3% move < 10% → suppressed under the new threshold
    expect(flash.classOf('x')).toBe('');
    flash.dispose();
  });
});

// ── Source-scan — confirms the real tickFlash.svelte.js ships the fix ────────

describe('tickFlash.svelte.js — Math.abs(last) source scan (item 10)', () => {
  it('update() guards the pctThreshold branch with Math.abs(last), not bare last', () => {
    expect(tickFlashSrc).toMatch(/if\s*\(_pctThreshold > 0 && Math\.abs\(last\) > 0\)/);
    expect(tickFlashSrc).not.toMatch(/if\s*\(_pctThreshold > 0 && last > 0\)/);
  });
});

// ── Accumulate-until-threshold fix (2026-10-19) ──────────────────────────────
//
// Root cause: comparing every incoming tick against the immediately
// preceding raw value only (never a fixed baseline) means several small
// same-direction ticks that together represent a real move never trigger a
// flash, because each individual tick-to-tick delta stays under threshold
// even though the cumulative move since the last visible flash crosses it.
// Most visible on GOLD (MCX, ~₹150,000/unit, ₹1 tick size — a single tick is
// ~0.0007%, ~140x below the default 0.1% gate).
//
// Fix: compare against the LAST VALUE THAT ACTUALLY TRIGGERED A FLASH, not
// the last raw tick — prev[key] is only written (a) on the very first
// sample (baseline establishment) and (b) right before a flash fires.

describe('createTickFlash — accumulate-until-threshold (GOLD small-tick fix)', () => {
  it('several consecutive sub-threshold updates accumulate and the LAST one fires the flash', () => {
    const flash = makeTickFlash({ threshold: 0, pctThreshold: 0.1, durationMs: 300 });
    // Seed baseline at 150000 (GOLD-like price).
    flash.update('GOLD', 150000);
    expect(flash.classOf('GOLD')).toBe('');

    // Five consecutive ~0.0007%/tick moves (₹1 ticks on a ₹150,000 base).
    // Each individual step is ~140x below the 0.1% threshold.
    flash.update('GOLD', 150001); // cumulative from baseline: 0.00067% — below threshold
    expect(flash.classOf('GOLD')).toBe('');
    flash.update('GOLD', 150002); // cumulative: 0.0013% — still below threshold
    expect(flash.classOf('GOLD')).toBe('');
    flash.update('GOLD', 150003); // cumulative: 0.002% — still below threshold
    expect(flash.classOf('GOLD')).toBe('');
    // Accumulate several more small ticks, staying just under the 0.1%
    // cumulative line (150140 / 150000 = 0.0933%).
    flash.update('GOLD', 150140);
    expect(flash.classOf('GOLD')).toBe('');
    // One more tick pushes cumulative move clearly past threshold — THIS is
    // the one that fires, not any of the earlier sub-threshold ticks.
    flash.update('GOLD', 150200); // cumulative from 150000: 0.133% ≥ 0.1% → flash
    expect(flash.classOf('GOLD')).toBe('tf-up');

    flash.dispose();
  });

  it('a single update crossing the threshold in one step still fires correctly (regression)', () => {
    const flash = makeTickFlash({ threshold: 0, pctThreshold: 0.1, durationMs: 300 });
    flash.update('x', 100);
    flash.update('x', 101); // 1% move ≥ 0.1% → flash immediately, no accumulation needed
    expect(flash.classOf('x')).toBe('tf-up');
    flash.dispose();
  });

  it('after a flash fires, the baseline resets — a subsequent small move needs fresh accumulation', () => {
    const flash = makeTickFlash({ threshold: 0, pctThreshold: 0.1, durationMs: 300 });
    flash.update('GOLD', 150000); // seed
    flash.update('GOLD', 150200); // 0.133% ≥ 0.1% → flashes, baseline resets to 150200
    expect(flash.classOf('GOLD')).toBe('tf-up');

    // Clear the flash class so the next assertion isn't polluted by the
    // still-active timer-driven class from the prior flash.
    vi.advanceTimersByTime(300);
    expect(flash.classOf('GOLD')).toBe('');

    // A small move relative to the NEW baseline (150200) must accumulate
    // fresh — NOT be measured against the original pre-flash baseline
    // (150000), which would make this a 0.2% cumulative move and fire
    // immediately instead of accumulating.
    flash.update('GOLD', 150250); // (150250-150200)/150200 = 0.033% — below threshold
    expect(flash.classOf('GOLD')).toBe('');

    // Confirm it still needs to cross threshold relative to the NEW
    // baseline (150200), not the original one.
    flash.update('GOLD', 150350); // (150350-150200)/150200 = 0.0999...% — still just below
    expect(flash.classOf('GOLD')).toBe('');
    flash.update('GOLD', 150400); // (150400-150200)/150200 = 0.133% ≥ 0.1% → flash
    expect(flash.classOf('GOLD')).toBe('tf-up');

    flash.dispose();
  });

  it('first sample still establishes baseline with no flash (unaffected by the accumulation fix)', () => {
    const flash = makeTickFlash({ threshold: 0, pctThreshold: 0.1, durationMs: 300 });
    flash.update('x', 100); // first sample — baseline only, no flash
    expect(flash.classOf('x')).toBe('');
    flash.dispose();
  });

  it('accumulation also works for a down-moving sequence', () => {
    const flash = makeTickFlash({ threshold: 0, pctThreshold: 0.1, durationMs: 300 });
    flash.update('GOLD', 150000);
    flash.update('GOLD', 149999); // tiny down-tick — below threshold
    expect(flash.classOf('GOLD')).toBe('');
    flash.update('GOLD', 149998); // still below threshold cumulative
    expect(flash.classOf('GOLD')).toBe('');
    flash.update('GOLD', 149800); // cumulative from 150000: -0.133% → flash down
    expect(flash.classOf('GOLD')).toBe('tf-down');
    flash.dispose();
  });
});

describe('tickFlash.svelte.js — accumulate-until-threshold source scan (2026-10-19)', () => {
  it('first-sample baseline write is the only unconditional prev[key] write', () => {
    expect(tickFlashSrc).toMatch(/if\s*\(last == null\)\s*\{\s*prev\[key\] = v;\s*return;\s*\}/);
  });

  it('prev[key] is not written immediately after reading last (no unconditional overwrite)', () => {
    expect(tickFlashSrc).not.toMatch(/const last = prev\[key\];\s*\n\s*prev\[key\] = v;/);
  });

  it('prev[key] is written again only after dir is computed (right before a flash fires)', () => {
    const dirIdx = tickFlashSrc.indexOf("const dir = v > last ? 'up' : 'down';");
    expect(dirIdx).toBeGreaterThan(-1);
    const afterDir = tickFlashSrc.slice(dirIdx);
    const nextPrevWriteIdx = afterDir.indexOf('prev[key] = v;');
    const classesWriteIdx = afterDir.indexOf('classes[key] = dir;');
    expect(nextPrevWriteIdx).toBeGreaterThan(-1);
    // The reset write must land between computing `dir` and setting the
    // classes[key] class — i.e. "right before the flash fires".
    expect(nextPrevWriteIdx).toBeLessThan(classesWriteIdx);
  });

  it('exactly two prev[key] write sites exist in update() — baseline seed and flash-reset', () => {
    const matches = tickFlashSrc.match(/prev\[key\] = v;/g) || [];
    expect(matches.length).toBe(2);
  });
});
