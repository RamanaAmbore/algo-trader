/**
 * chartCrosshairAudit.test.js
 *
 * Source-audit guard for the Wave 1/2 shared-crosshair consolidation.
 * Before this change, 7 hand-rolled SVG charts each wrote their own
 * inline crosshair <line>/<circle> markup — 4 different stroke colors,
 * 2 widths, 3 dash patterns, inconsistent dot presence. Wave 1 builds
 * `ChartCrosshair.svelte` as the single canonical implementation and
 * migrates the 2 reference charts (ChartWorkspace — the operator-named
 * "good" look — and OptionsPayoff — the most visibly inconsistent).
 *
 * This is a deterministic static-source check (no live data / mount
 * harness needed — see InfoHint.sourceAudit.test.js for the same
 * pattern in this repo). It proves two things no amount of live-browser
 * testing alone can guarantee:
 *   1. The old bespoke inline crosshair markup is actually GONE from
 *      both migrated files (not just added-alongside).
 *   2. Both files render <ChartCrosshair ...> and the component itself
 *      still carries the exact canonical stroke/dash/width string that
 *      was ChartWorkspace's own pre-migration look — so a future editor
 *      can't silently drift the shared component's hardcoded style.
 *
 * Runtime/visual verification (hover-triggered computed style on both
 * charts) lives in e2e/chart_crosshair_consistency.spec.js.
 */

import { describe, it, expect } from 'vitest';
import CROSSHAIR_SRC from '../ChartCrosshair.svelte?raw';
import WORKSPACE_SRC from '../ChartWorkspace.svelte?raw';
import PAYOFF_SRC from '../OptionsPayoff.svelte?raw';

describe('ChartCrosshair.svelte — canonical line styling', () => {
  it('hardcodes the canonical amber dashed stroke (ChartWorkspace\'s own pre-migration look)', () => {
    expect(CROSSHAIR_SRC).toContain('stroke="rgba(251,191,36,0.5)"');
    expect(CROSSHAIR_SRC).toContain('stroke-width="1"');
    expect(CROSSHAIR_SRC).toContain('stroke-dasharray="3 2"');
  });

  it('does not expose a color/stroke/dash prop for the crosshair line itself', () => {
    // Only mode / showDot / dotColor / dotStroke / x / y / bounds are
    // legitimate per-call-site knobs — a `color`/`stroke`/`dash` prop
    // would defeat the component's entire purpose.
    expect(CROSSHAIR_SRC).not.toMatch(/\bstroke\s*=\s*(stroke|color|dash)\b/);
    expect(CROSSHAIR_SRC).toMatch(/mode\s*=\s*'vertical'/);
    expect(CROSSHAIR_SRC).toMatch(/showDot\s*=\s*true/);
    expect(CROSSHAIR_SRC).toMatch(/dotColor\s*=\s*'#fbbf24'/);
    expect(CROSSHAIR_SRC).toMatch(/dotStroke\s*=\s*'#fff'/);
  });

  it('renders no own <svg> wrapper — markup-only, meant to inherit the caller\'s SVG', () => {
    // Slice past the top-of-file doc comment first — its prose
    // legitimately mentions "<svg>" when explaining the design intent.
    const afterComment = CROSSHAIR_SRC.slice(CROSSHAIR_SRC.indexOf('-->') + 3);
    expect(afterComment).not.toContain('<svg');
  });
});

describe('ChartWorkspace.svelte — migrated to shared ChartCrosshair', () => {
  it('imports ChartCrosshair', () => {
    expect(WORKSPACE_SRC).toMatch(/import ChartCrosshair from '\$lib\/ChartCrosshair\.svelte';/);
  });

  it('renders <ChartCrosshair ...> at the hover-crosshair call site', () => {
    expect(WORKSPACE_SRC).toContain('<ChartCrosshair');
  });

  it('no longer has its own inline crosshair stroke/dash markup', () => {
    // The old inline crosshair <line> carried this exact stroke+dash pair
    // together — if both strings still co-occur, the bespoke markup
    // survived instead of being replaced.
    expect(WORKSPACE_SRC).not.toMatch(
      /stroke="rgba\(251,191,36,0\.5\)" stroke-width="1" stroke-dasharray="3 2"/
    );
  });

  it('migrated its x-axis major gridline to the shared .chart-grid-line class', () => {
    // The old inline gridline carried this exact color-mix stroke value
    // directly on the <line> element — must now go through class=.
    expect(WORKSPACE_SRC).not.toMatch(
      /stroke="color-mix\(in srgb, var\(--algo-slate\) 10%, transparent\)" stroke-width="1" stroke-dasharray="2 3"/
    );
    expect(WORKSPACE_SRC).toMatch(/class="chart-grid-line"/);
  });
});

describe('OptionsPayoff.svelte — migrated to shared ChartCrosshair', () => {
  it('imports ChartCrosshair', () => {
    expect(PAYOFF_SRC).toMatch(/import ChartCrosshair from '\$lib\/ChartCrosshair\.svelte';/);
  });

  it('renders <ChartCrosshair ...> at the hover-crosshair call site', () => {
    expect(PAYOFF_SRC).toContain('<ChartCrosshair');
  });

  it('no longer has its own bespoke white/solid crosshair line', () => {
    // This was the most visibly inconsistent implementation — plain
    // white, no dash, no dot. Must be fully gone, not left alongside
    // the new component.
    expect(PAYOFF_SRC).not.toMatch(/stroke="rgba\(255,255,255,0\.20\)" stroke-width="1"/);
  });

  it('passes showDot={false} (documented: preserveAspectRatio="none" bg svg + multi-curve chart + existing hover overlay already marks the point)', () => {
    const idx = PAYOFF_SRC.indexOf('<ChartCrosshair');
    const callSite = PAYOFF_SRC.slice(idx, PAYOFF_SRC.indexOf('/>', idx));
    expect(callSite).toMatch(/showDot=\{false\}/);
    expect(callSite).toMatch(/mode="vertical"/);
  });
});
