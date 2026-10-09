// 2026-10 P0 fix — the LIVE execution-mode pill rendered GREEN in
// ChaseCard.svelte and OrderTimelineDrawer.svelte but RED everywhere
// else (LogPanel.svelte, the navbar). SIM and REPLAY are *also* green in
// several places, so a green LIVE pill was indistinguishable from a
// safe/sandbox mode — an operator could misread it as "not real money"
// when LIVE is the one mode that moves real money.
//
// Canonical scheme lives in the navbar's MODE_COLOR map
// (`frontend/src/routes/(algo)/+layout.svelte`): idle=slate,
// sim=green, replay=green, paper=sky, shadow=orange, live=red. This
// spec locks that mapping as a source-level regression guard across all
// four mode-pill surfaces, plus the pill-shape (2px radius) unification.
//
// Source-level guard, same pattern as held_orders_card_styling.spec.js
// and automation_agent_row_card_styling.spec.js — regex over the raw
// component source, no browser/DOM needed.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const layoutSrc = readFileSync(
  new URL('../src/routes/(algo)/+layout.svelte', import.meta.url).pathname, 'utf8'
);
const chaseCardSrc = readFileSync(
  new URL('../src/lib/order/ChaseCard.svelte', import.meta.url).pathname, 'utf8'
);
const otdSrc = readFileSync(
  new URL('../src/lib/order/OrderTimelineDrawer.svelte', import.meta.url).pathname, 'utf8'
);
const logPanelSrc = readFileSync(
  new URL('../src/lib/LogPanel.svelte', import.meta.url).pathname, 'utf8'
);

test.describe('mode-pill color consistency — LIVE is red everywhere, never green', () => {
  test('navbar MODE_COLOR is the canonical reference: live=red, sim/replay=green', () => {
    const map = layoutSrc.match(/const MODE_COLOR = \{[^}]*\}/s)?.[0] ?? '';
    expect(map).not.toBe('');
    expect(map).toMatch(/live:\s*'var\(--c-short\)'/);
    expect(map).toMatch(/sim:\s*'var\(--c-long\)'/);
    expect(map).toMatch(/replay:\s*'var\(--c-long\)'/);
    expect(map).toMatch(/paper:\s*'#7dd3fc'/);
    expect(map).toMatch(/shadow:\s*'#fb923c'/);
  });

  test('ChaseCard .cc-mode-live resolves to --c-short, not --c-long or a green literal', () => {
    const rule = chaseCardSrc.match(/\.cc-mode-live\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).not.toBe('');
    expect(rule).toMatch(/var\(--c-short\)/);
    expect(rule).not.toMatch(/var\(--c-long\)/);
    expect(rule).not.toMatch(/#4ade80/);
    expect(rule).not.toMatch(/74,\s*222,\s*128/);
  });

  test('OrderTimelineDrawer .otd-mode-live resolves to --c-short, not --c-long or a green literal', () => {
    const rule = otdSrc.match(/\.otd-mode-live\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).not.toBe('');
    expect(rule).toMatch(/var\(--c-short\)/);
    expect(rule).not.toMatch(/var\(--c-long\)/);
    expect(rule).not.toMatch(/#4ade80/);
    expect(rule).not.toMatch(/74,\s*222,\s*128/);
  });

  test('LogPanel .mode-pill-live and the om-chip-live filter chip are red (already correct — regression guard)', () => {
    const pill = logPanelSrc.match(/:global\(\.mode-pill-live\)\s*\{[^}]*\}/)?.[0] ?? '';
    const chip = logPanelSrc.match(/\.om-chip\.om-on\.om-chip-live\s*\{[^}]*\}/)?.[0] ?? '';
    expect(pill).not.toBe('');
    expect(chip).not.toBe('');
    for (const rule of [pill, chip]) {
      expect(rule).toMatch(/var\(--c-short\)/);
      expect(rule).not.toMatch(/var\(--c-long\)/);
      expect(rule).not.toMatch(/#4ade80/);
      expect(rule).not.toMatch(/74,\s*222,\s*128/);
    }
  });

  test('no file attaches --c-long or a green literal to any live-mode class', () => {
    for (const [name, src, selector] of [
      ['ChaseCard', chaseCardSrc, /\.cc-mode-live\s*\{[^}]*\}/],
      ['OrderTimelineDrawer', otdSrc, /\.otd-mode-live\s*\{[^}]*\}/],
      ['LogPanel pill', logPanelSrc, /:global\(\.mode-pill-live\)\s*\{[^}]*\}/],
      ['LogPanel chip', logPanelSrc, /\.om-chip\.om-on\.om-chip-live\s*\{[^}]*\}/],
    ]) {
      const rule = src.match(selector)?.[0] ?? '';
      expect(rule, `${name} live rule should exist`).not.toBe('');
      expect(rule, `${name} must not use --c-long on live`).not.toMatch(/--c-long\b/);
      expect(rule, `${name} must not use a green literal on live`).not.toMatch(/#4ade80|74,\s*222,\s*128/);
    }
  });
});

test.describe('mode-pill color consistency — SIM/REPLAY/SHADOW/PAPER unified to canonical hues', () => {
  test('ChaseCard: sim/replay mapped to green, shadow to orange, paper to sky, via _modeCls', () => {
    expect(chaseCardSrc).toMatch(/if \(k === 'sim'\)\s*return 'cc-mode cc-mode-sim'/);
    expect(chaseCardSrc).toMatch(/if \(k === 'replay'\)\s*return 'cc-mode cc-mode-replay'/);
    const sim = chaseCardSrc.match(/\.cc-mode-sim\s*\{[^}]*\}/)?.[0] ?? '';
    const replay = chaseCardSrc.match(/\.cc-mode-replay\s*\{[^}]*\}/)?.[0] ?? '';
    const shadow = chaseCardSrc.match(/\.cc-mode-shadow\s*\{[^}]*\}/)?.[0] ?? '';
    const paper = chaseCardSrc.match(/\.cc-mode-paper\s*\{[^}]*\}/)?.[0] ?? '';
    expect(sim).toMatch(/var\(--c-long\)/);
    expect(replay).toMatch(/var\(--c-long\)/);
    expect(shadow).toMatch(/var\(--algo-orange\)/);
    expect(paper).toMatch(/var\(--algo-sky\)/);
  });

  test('OrderTimelineDrawer: shadow/replay branches exist (previously fell through to unknown)', () => {
    expect(otdSrc).toMatch(/if \(mode === 'shadow'\)\s*return 'otd-mode-shadow'/);
    expect(otdSrc).toMatch(/if \(mode === 'replay'\)\s*return 'otd-mode-replay'/);
    const shadow = otdSrc.match(/\.otd-mode-shadow\s*\{[^}]*\}/)?.[0] ?? '';
    const replay = otdSrc.match(/\.otd-mode-replay\s*\{[^}]*\}/)?.[0] ?? '';
    const sim = otdSrc.match(/\.otd-mode-sim\s*\{[^}]*\}/)?.[0] ?? '';
    expect(shadow).not.toBe('');
    expect(replay).not.toBe('');
    expect(shadow).toMatch(/var\(--algo-orange\)/);
    expect(replay).toMatch(/var\(--c-long\)/);
    // sim was amber pre-fix — now unified to green, matching replay.
    expect(sim).toMatch(/var\(--c-long\)/);
    expect(sim).not.toMatch(/var\(--c-action\)/);
  });

  test('OrderTimelineDrawer paper pill no longer uses the mismatched #38bdf8 literal', () => {
    const paper = otdSrc.match(/\.otd-mode-paper\s*\{[^}]*\}/)?.[0] ?? '';
    expect(paper).not.toBe('');
    expect(paper).toMatch(/var\(--algo-sky\)/);
    expect(paper).not.toMatch(/#38bdf8/);
    expect(paper).not.toMatch(/56,\s*189,\s*248/);
  });

  test('LogPanel: sim pill + chip unified to green (was amber), paper pill + chip use --algo-sky family (was a mismatched blue literal)', () => {
    const simPill = logPanelSrc.match(/:global\(\.mode-pill-sim\)\s*\{[^}]*\}/)?.[0] ?? '';
    const simChip = logPanelSrc.match(/\.om-chip\.om-on\.om-chip-sim\s*\{[^}]*\}/)?.[0] ?? '';
    expect(simPill).toMatch(/var\(--c-long\)/);
    expect(simChip).toMatch(/var\(--c-long\)/);

    const paperPill = logPanelSrc.match(/:global\(\.mode-pill-paper\)\s*\{[^}]*\}/)?.[0] ?? '';
    const paperChip = logPanelSrc.match(/\.om-chip\.om-on\.om-chip-paper\s*\{[^}]*\}/)?.[0] ?? '';
    for (const rule of [paperPill, paperChip]) {
      expect(rule).not.toBe('');
      expect(rule).toMatch(/var\(--algo-sky/); // --algo-sky / --algo-sky-bg / --algo-sky-border
      expect(rule).not.toMatch(/#38bdf8/);
      expect(rule).not.toMatch(/56,\s*189,\s*248/);
    }
  });
});

test.describe('mode-pill shape unification — 2px radius everywhere, not a mix of 2px and fully-rounded', () => {
  test('OrderTimelineDrawer .otd-mode-pill uses a 2px radius (was 9999px / fully rounded)', () => {
    const rule = otdSrc.match(/\.otd-mode-pill\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).not.toBe('');
    expect(rule).toMatch(/border-radius:\s*2px/);
    expect(rule).not.toMatch(/border-radius:\s*9999px/);
  });

  test('LogPanel .mode-pill and ChaseCard .cc-mode already use a 2px radius (regression guard)', () => {
    const logRule = logPanelSrc.match(/:global\(\.mode-pill\)\s*\{[^}]*\}/)?.[0] ?? '';
    const ccRule = chaseCardSrc.match(/\.cc-mode\s*\{[^}]*\}/)?.[0] ?? '';
    expect(logRule).toMatch(/border-radius:\s*2px/);
    expect(ccRule).toMatch(/border-radius:\s*2px/);
  });
});

test.describe('DRAFT mode pill — unified to muted/dashed (was amber in ChaseCard, muted-dashed elsewhere)', () => {
  test('ChaseCard .cc-mode-draft is muted + dashed, not amber', () => {
    const rule = chaseCardSrc.match(/\.cc-mode-draft\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).not.toBe('');
    expect(rule).toMatch(/var\(--algo-muted\)/);
    expect(rule).toMatch(/border-style:\s*dashed/);
    expect(rule).not.toMatch(/var\(--c-action\)/);
  });

  test('LogPanel .mode-pill-draft and OrderTimelineDrawer .otd-mode-draft are muted + dashed (regression guard)', () => {
    const logRule = logPanelSrc.match(/:global\(\.mode-pill-draft\)\s*\{[^}]*\}/)?.[0] ?? '';
    const otdRule = otdSrc.match(/\.otd-mode-draft\s*\{[^}]*\}/)?.[0] ?? '';
    for (const rule of [logRule, otdRule]) {
      expect(rule).toMatch(/var\(--algo-muted\)/);
      expect(rule).toMatch(/border-style:\s*dashed/);
    }
  });
});
