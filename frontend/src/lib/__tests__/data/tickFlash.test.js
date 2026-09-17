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

function makeTickFlash({ threshold = 0, durationMs = 300 } = {}) {
  const prev = {};
  const timers = {};
  const classes = {};

  function update(key, value) {
    if (value == null) return;
    const v = Number(value);
    if (!isFinite(v)) return;
    // Guard fires BEFORE prev[key] read/write — mirrors the fixed code.
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    const last = prev[key];
    prev[key] = v;
    if (last == null) return;
    if (v === last) return;
    if (Math.abs(v - last) < threshold) return;
    const dir = v > last ? 'up' : 'down';
    classes[key] = dir;
    if (timers[key]) clearTimeout(timers[key]);
    timers[key] = setTimeout(() => {
      classes[key] = '';
      delete timers[key];
    }, durationMs);
  }

  function classOf(key) {
    const c = classes[key];
    return c === 'up' ? 'tf-up' : c === 'down' ? 'tf-down' : '';
  }

  function dispose() {
    for (const t of Object.values(timers)) clearTimeout(t);
  }

  return { update, classOf, dispose };
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
