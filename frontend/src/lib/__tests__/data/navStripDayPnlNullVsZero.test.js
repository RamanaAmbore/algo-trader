/**
 * navStripDayPnlNullVsZero.test.js
 *
 * Real-money fix (2026-10): NavStrip's P∆/H∆ pills (dispPositionsToday /
 * dispHoldingsToday in PositionStrip.svelte) now distinguish "no poll has
 * ever landed this session" (null — render `—`) from "broker confirmed a
 * genuinely flat book" (a real `0`) — matching the pre-existing margin/cash
 * pill convention. This also covers an operator-requested refinement to the
 * market-open/mode-switch reset: a genuine closed→open session transition
 * still resets the day-delta to a real `0` (mathematically correct — no
 * price move has happened since session open yet); an execution-mode
 * switch (navbar picker) no longer resets at all — it freezes the prior
 * value until the post-reload branch updates it in place.
 *
 * PositionStrip.svelte is a Svelte 5 component and can't be imported
 * directly into Vitest (no Svelte compiler registered — same established
 * convention as portfolioStore.test.js's own R4a source-grep guard and
 * liveHoldingsValue.test.js's pure-mirror pattern). This file: (1)
 * source-greps the real shipped component so a regression in the actual
 * wiring is caught, and (2) exercises a pure-function mirror of the two
 * branches' logic directly.
 *
 * Five quality dimensions:
 *   1. SSOT   — mirrors the exact branch logic in PositionStrip.svelte
 *   2. Perf   — pure text scan + pure functions, no DOM/Svelte compilation
 *   3. Stale  — covers degraded/never-polled vs confirmed-empty vs real value
 *   4. Reuse  — reused mirror functions for both open-transition and
 *               post-reload branches, matching the real component's split
 *   5. UX     — the exact operator-visible outcome (— vs 0 vs frozen prior
 *               value) for each of the three scenarios this fix addresses
 */

import { describe, it, expect } from 'vitest';
// @ts-ignore — Vite raw-import suffix has no TS module declaration here.
import positionStripSrc from '../../PositionStrip.svelte?raw';

// ── Source-grep guards (real file) ──────────────────────────────────────────

describe('PositionStrip.svelte — dispPositionsToday/dispHoldingsToday init via ?? null, not || 0 (real file)', () => {
  it('both $state inits use ?? null', () => {
    expect(positionStripSrc).toMatch(/let dispPositionsToday = \$state\(positionsDayPnlStore\.total \?\? null\);/);
    expect(positionStripSrc).toMatch(/let dispHoldingsToday\s*= \$state\(holdingsDayPnlStore\.total \?\? null\);/);
  });

  it('neither init uses the old || 0 coercion', () => {
    expect(positionStripSrc).not.toMatch(/\$state\(positionsDayPnlStore\.total \|\| 0\)/);
    expect(positionStripSrc).not.toMatch(/\$state\(holdingsDayPnlStore\.total \|\| 0\)/);
  });
});

describe('PositionStrip.svelte — open-transition vs mode-switch reset is split, not shared (real file)', () => {
  it('declares an explicit _isOpenTransition flag distinguishing the two triggers', () => {
    expect(positionStripSrc).toMatch(/const _isOpenTransition = open && !_prevMktOpen;/);
  });

  it('the dispPositionsToday/dispHoldingsToday = 0 reset is scoped to the _isOpenTransition branch only', () => {
    const idx = positionStripSrc.indexOf('if (_isOpenTransition) {');
    expect(idx, '_isOpenTransition branch not found').toBeGreaterThan(-1);
    const blockEnd = positionStripSrc.indexOf('}', idx);
    const block = positionStripSrc.slice(idx, blockEnd);
    expect(block).toContain('dispPositionsToday = 0;');
    expect(block).toContain('dispHoldingsToday  = 0;');
  });

  it('cachedDelete/_openTransitionAt/_load still run for BOTH triggers (outside the _isOpenTransition-only inner block)', () => {
    const outerIdx = positionStripSrc.indexOf('if (_isOpenTransition || modeChanged) {');
    expect(outerIdx, 'outer combined-trigger branch not found').toBeGreaterThan(-1);
    const outerEnd = positionStripSrc.indexOf('\n    }', outerIdx);
    const outerBlock = positionStripSrc.slice(outerIdx, outerEnd);
    expect(outerBlock).toContain("cachedDelete('strip.frozen');");
    expect(outerBlock).toContain('_openTransitionAt = Date.now();');
    expect(outerBlock).toContain('_load();');
  });
});

describe('PositionStrip.svelte — post-reload branch distinguishes null (never-polled) from confirmed-empty 0 (real file)', () => {
  it('positions branch checks newPTotal != null (not !== 0)', () => {
    expect(positionStripSrc).toMatch(/const newPTotal = positionsDayPnlStore\.total;\s*\n\s*if \(newPTotal != null\) \{/);
  });

  it('positions branch requires positionsStore.value != null for the confirmed-empty-0 fallback', () => {
    expect(positionStripSrc).toMatch(/positions\.length === 0 && !positionsStore\.meta\?\.degraded && positionsStore\.value != null/);
  });

  it('positions branch falls through to null (not frozen, not a stale 0) when genuinely unknown', () => {
    const idx = positionStripSrc.indexOf('const newPTotal = positionsDayPnlStore.total;');
    expect(idx, 'positions post-reload block not found').toBeGreaterThan(-1);
    const blockEnd = positionStripSrc.indexOf('\n    }', idx);
    const block = positionStripSrc.slice(idx, blockEnd);
    expect(block).toContain('dispPositionsToday = null;');
  });

  it('holdings branch mirrors the identical null-vs-confirmed-empty-0 structure', () => {
    expect(positionStripSrc).toMatch(/const newHTotal = holdingsDayPnlStore\.total;\s*\n\s*if \(newHTotal != null\) \{/);
    expect(positionStripSrc).toMatch(/holdings\.length === 0 && !pulseHoldingsStore\.meta\?\.degraded && pulseHoldingsStore\.value != null/);
    const idx = positionStripSrc.indexOf('const newHTotal = holdingsDayPnlStore.total;');
    const blockEnd = positionStripSrc.indexOf('\n    }', idx);
    const block = positionStripSrc.slice(idx, blockEnd);
    expect(block).toContain('dispHoldingsToday = null;');
  });
});

// ── Pure-function mirrors ───────────────────────────────────────────────────

/**
 * Mirror of PositionStrip.svelte's open-transition / mode-switch split.
 * @param {{open: boolean, prevMktOpen: boolean, modeChanged: boolean, dispP: number|null, dispH: number|null}} args
 * @returns {{dispP: number|null, dispH: number|null, loaded: boolean}}
 */
function mirrorOpenTransitionOrModeSwitch({ open, prevMktOpen, modeChanged, dispP, dispH }) {
  const isOpenTransition = open && !prevMktOpen;
  let loaded = false;
  if (isOpenTransition || modeChanged) {
    if (isOpenTransition) {
      dispP = 0;
      dispH = 0;
    }
    loaded = true; // stands in for cachedDelete + _load()
  }
  return { dispP, dispH, loaded };
}

/**
 * Mirror of PositionStrip.svelte's post-reload pFresh/hFresh branch (positions
 * side — holdings mirrors identically with the h-prefixed names).
 * @param {{pFresh: boolean, newPTotal: number|null, positionsLen: number, degraded: boolean, storeValueIsNull: boolean, dispP: number|null}} args
 */
function mirrorPostReloadPositions({ pFresh, newPTotal, positionsLen, degraded, storeValueIsNull, dispP }) {
  if (!pFresh) return dispP;
  if (newPTotal != null) return newPTotal;
  if (positionsLen === 0 && !degraded && !storeValueIsNull) return 0;
  return null;
}

describe('mirror — open-transition resets day-delta to a real 0 (mathematically correct)', () => {
  it('closed → open transition resets both pills to 0, even if they were previously null/—', () => {
    const r = mirrorOpenTransitionOrModeSwitch({ open: true, prevMktOpen: false, modeChanged: false, dispP: null, dispH: null });
    expect(r.dispP).toBe(0);
    expect(r.dispH).toBe(0);
    expect(r.loaded).toBe(true);
  });

  it('closed → open transition resets both pills to 0 even if they previously held a real nonzero value', () => {
    const r = mirrorOpenTransitionOrModeSwitch({ open: true, prevMktOpen: false, modeChanged: false, dispP: 5000, dispH: -2000 });
    expect(r.dispP).toBe(0);
    expect(r.dispH).toBe(0);
  });
});

describe('mirror — mode switch FREEZES the prior value, never resets to 0 or null', () => {
  it('a mode switch while market is already open leaves both pills untouched', () => {
    const r = mirrorOpenTransitionOrModeSwitch({ open: true, prevMktOpen: true, modeChanged: true, dispP: 1234, dispH: 567 });
    expect(r.dispP).toBe(1234); // frozen, not reset to 0
    expect(r.dispH).toBe(567);
    expect(r.loaded).toBe(true); // _load() still fires so fresh data arrives
  });

  it('a mode switch while the pills were null (never polled) leaves them null, not flashed to 0', () => {
    const r = mirrorOpenTransitionOrModeSwitch({ open: true, prevMktOpen: true, modeChanged: true, dispP: null, dispH: null });
    expect(r.dispP).toBeNull();
    expect(r.dispH).toBeNull();
  });

  it('neither open-transition nor mode-switch: both pills untouched, no load triggered', () => {
    const r = mirrorOpenTransitionOrModeSwitch({ open: true, prevMktOpen: true, modeChanged: false, dispP: 42, dispH: 99 });
    expect(r.dispP).toBe(42);
    expect(r.dispH).toBe(99);
    expect(r.loaded).toBe(false);
  });
});

describe('mirror — post-reload branch: null (never-polled) vs 0 (confirmed-flat) vs real value', () => {
  it('not fresh yet: pill stays at its previous value (not overwritten)', () => {
    expect(mirrorPostReloadPositions({ pFresh: false, newPTotal: 777, positionsLen: 0, degraded: false, storeValueIsNull: false, dispP: 42 })).toBe(42);
  });

  it('fresh + a real nonzero total: pill takes the real value', () => {
    expect(mirrorPostReloadPositions({ pFresh: true, newPTotal: 555, positionsLen: 3, degraded: false, storeValueIsNull: false, dispP: null })).toBe(555);
  });

  it('fresh + a real confirmed 0 total: pill takes the real 0 (not re-derived from positionsLen)', () => {
    expect(mirrorPostReloadPositions({ pFresh: true, newPTotal: 0, positionsLen: 3, degraded: false, storeValueIsNull: false, dispP: null })).toBe(0);
  });

  it('fresh + null total + confirmed-empty book (poll landed, 0 positions, not degraded): renders a real 0', () => {
    expect(mirrorPostReloadPositions({ pFresh: true, newPTotal: null, positionsLen: 0, degraded: false, storeValueIsNull: false, dispP: null })).toBe(0);
  });

  it('fresh + null total + degraded: falls through to null (genuinely unknown), never a stale 0', () => {
    expect(mirrorPostReloadPositions({ pFresh: true, newPTotal: null, positionsLen: 0, degraded: true, storeValueIsNull: false, dispP: 100 })).toBeNull();
  });

  it('fresh + null total + store value itself never landed (cold start, pre-poll placeholder): falls through to null', () => {
    expect(mirrorPostReloadPositions({ pFresh: true, newPTotal: null, positionsLen: 0, degraded: false, storeValueIsNull: true, dispP: null })).toBeNull();
  });
});
