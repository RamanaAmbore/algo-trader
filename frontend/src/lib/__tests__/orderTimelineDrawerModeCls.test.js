/**
 * orderTimelineDrawerModeCls.test.js
 *
 * Unit tests for the `modeCls(mode)` fix in OrderTimelineDrawer.svelte.
 *
 * modeCls already fell back safely for an unrecognized mode
 * ('otd-mode-unknown', not live — the drawer's own section.mode text is
 * rendered verbatim uppercased, so nothing was ever mislabeled). This is
 * a polish/consistency pass: draft now gets its own explicit branch
 * ('otd-mode-draft', dashed + muted) instead of falling into the generic
 * unknown (solid slate) styling, matching LogPanel's new DRAFT pill.
 *
 * These tests replicate `modeCls` as a standalone pure function —
 * matching the existing pure-function test pattern in
 * ChaseCard.inflight.test.js and orderStatusPredicates.test.js — since
 * the function is a `<script>`-scoped const inside
 * OrderTimelineDrawer.svelte, not an importable module.
 *
 * Five quality dimensions:
 *   1. SSOT   — one function backs the mode pill class for every
 *               section header in the drawer.
 *   2. Perf   — pure unit tests, no DOM / network.
 *   3. Stale  — the old (unknown-for-draft) behaviour is tested first to
 *               document it, then the fixed explicit-draft branch.
 *   4. Reuse  — one factory exercised against every known mode plus an
 *               unrecognized one.
 *   5. UX     — draft must render visually distinct from both a real
 *               mode AND the generic "truly unknown" state, so an
 *               operator scanning the drawer can tell them apart.
 */

import { describe, it, expect } from 'vitest';

// Mirrors the OLD (pre-polish) implementation — draft fell into the
// generic unknown bucket, same as a truly unrecognized mode.
function modeClsOld(mode) {
  if (mode === 'sim')   return 'otd-mode-sim';
  if (mode === 'paper') return 'otd-mode-paper';
  if (mode === 'live')  return 'otd-mode-live';
  return 'otd-mode-unknown';
}

// Replicated from OrderTimelineDrawer.svelte's fixed modeCls — keep in sync.
// Audit fix (mode-pill color consistency): shadow/replay branches added —
// previously both fell through to otd-mode-unknown (no CSS existed for
// them at all).
function modeCls(mode) {
  if (mode === 'sim')    return 'otd-mode-sim';
  if (mode === 'paper')  return 'otd-mode-paper';
  if (mode === 'live')   return 'otd-mode-live';
  if (mode === 'shadow') return 'otd-mode-shadow';
  if (mode === 'replay') return 'otd-mode-replay';
  if (mode === 'draft')  return 'otd-mode-draft';
  return 'otd-mode-unknown';
}

describe('OrderTimelineDrawer modeCls — pre-polish behaviour (documents the gap)', () => {
  it('draft used to be indistinguishable from a truly unknown mode', () => {
    expect(modeClsOld('draft')).toBe('otd-mode-unknown');
    expect(modeClsOld('totally-bogus')).toBe('otd-mode-unknown');
  });
});

describe('OrderTimelineDrawer modeCls — fixed behaviour', () => {
  it('known modes map to their own class', () => {
    expect(modeCls('sim')).toBe('otd-mode-sim');
    expect(modeCls('paper')).toBe('otd-mode-paper');
    expect(modeCls('live')).toBe('otd-mode-live');
    expect(modeCls('shadow')).toBe('otd-mode-shadow');
    expect(modeCls('replay')).toBe('otd-mode-replay');
  });

  it('draft gets its own explicit class, distinct from both live and unknown', () => {
    expect(modeCls('draft')).toBe('otd-mode-draft');
    expect(modeCls('draft')).not.toBe('otd-mode-live');
    expect(modeCls('draft')).not.toBe('otd-mode-unknown');
  });

  it('a genuinely unrecognized mode still falls back to unknown, never live', () => {
    expect(modeCls('bogus')).toBe('otd-mode-unknown');
    expect(modeCls(undefined)).toBe('otd-mode-unknown');
    expect(modeCls(null)).toBe('otd-mode-unknown');
  });
});
