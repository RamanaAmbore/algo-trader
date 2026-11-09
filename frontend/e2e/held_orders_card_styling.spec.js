// 2026-10 P2 cleanup: HeldOrdersCard previously rendered all three hold
// categories (Bracket exit / Expiry close / Repeated rejection) with the
// exact same --algo-muted color, used hand-written rgba literals instead
// of app.css tokens for its border/buttons, and had no InfoHint explaining
// what "held" means. Source-level guard, same pattern as
// held_orders_release_all_labels.spec.js.
//
// 2026-11 (Track 3, T3.4, Bracket/Template/GTT removal): the "Bracket
// exit" category / .held-kind-bracket class were removed.
//
// 2026-11 (post-removal audit fix): `kindOf()`/`kindClassOf()` silently
// mislabeled the new (Track 1) `agent_close` hold category — the
// exit-tp-short-option-90 agent's require_release hold — as "Expiry
// close". Added a dedicated 'Profit-target close' / .held-kind-agent-close
// branch. Three kinds now: Expiry close, Repeated rejection,
// Profit-target close.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const cardSrc = readFileSync(
  new URL('../src/lib/HeldOrdersCard.svelte', import.meta.url).pathname, 'utf8'
);

test.describe('held orders card — per-category coloring + token usage', () => {
  test('each hold kind maps to its own CSS class, not a shared one', () => {
    expect(cardSrc).toMatch(/function kindClassOf\(row\)/);
    expect(cardSrc).not.toMatch(/'held-kind-bracket'/);
    expect(cardSrc).toMatch(/'held-kind-rejection'/);
    expect(cardSrc).toMatch(/'held-kind-expiry'/);
    expect(cardSrc).toMatch(/'held-kind-agent-close'/);
  });

  test("agent_close hold category resolves to its own label, not the Expiry-close fallback", () => {
    expect(cardSrc).toMatch(/h\.category === 'agent_close'/);
    expect(cardSrc).toMatch(/return 'Profit-target close'/);
  });

  test('the three hold-kind classes resolve to three different color tokens', () => {
    expect(cardSrc).not.toMatch(/\.held-kind-bracket\s*\{/);
    const expiry = cardSrc.match(/\.held-kind-expiry\s*\{[^}]*\}/)?.[0] ?? '';
    const rejection = cardSrc.match(/\.held-kind-rejection\s*\{[^}]*\}/)?.[0] ?? '';
    const agentClose = cardSrc.match(/\.held-kind-agent-close\s*\{[^}]*\}/)?.[0] ?? '';
    expect(expiry).toMatch(/var\(--algo-amber\)/);
    expect(rejection).toMatch(/var\(--algo-orange\)/);
    expect(agentClose).toMatch(/var\(--algo-green\)/);
    // No two must be the same literal color repeated.
    expect(new Set([expiry, rejection, agentClose]).size).toBe(3);
  });

  test('no hand-written rgba(251,191,36,...) literal remains on .held-card', () => {
    const rule = cardSrc.match(/\.held-card\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).not.toMatch(/rgba\(251,\s*191,\s*36/);
    expect(rule).toMatch(/var\(--algo-amber-border-soft\)/);
    expect(rule).toMatch(/var\(--btn-radius\)/);
  });

  test('release/cancel/release-all buttons use --btn-amber-*/--btn-sell-* and --btn-disabled-opacity, not hand-written literals', () => {
    const release = cardSrc.match(/\.held-release\s*\{[^}]*\}/)?.[0] ?? '';
    const cancel = cardSrc.match(/\.held-cancel\s*\{[^}]*\}/)?.[0] ?? '';
    const releaseAllBtn = cardSrc.match(/\.held-release-all\s*\{[^}]*\}/)?.[0] ?? '';
    for (const rule of [release, releaseAllBtn]) {
      expect(rule).toMatch(/var\(--btn-amber\)/);
      expect(rule).toMatch(/var\(--btn-amber-border\)/);
      expect(rule).toMatch(/var\(--btn-radius\)/);
    }
    expect(cancel).toMatch(/var\(--btn-sell\)/);
    expect(cancel).toMatch(/var\(--btn-sell-border\)/);
    const disabledRules = cardSrc.match(/:disabled\s*\{[^}]*\}/g) ?? [];
    expect(disabledRules.length).toBeGreaterThan(0);
    for (const rule of disabledRules) {
      expect(rule).toMatch(/var\(--btn-disabled-opacity\)/);
      expect(rule).not.toMatch(/opacity:\s*0\.5/);
    }
  });

  test('buttons get a hover state (previously none)', () => {
    expect(cardSrc).toMatch(/\.held-release:hover:not\(:disabled\)/);
    expect(cardSrc).toMatch(/\.held-cancel:hover:not\(:disabled\)/);
    expect(cardSrc).toMatch(/\.held-release-all:hover:not\(:disabled\)/);
  });

  test('side (BUY/SELL) is colored, matching the rest of the app\'s buy/sell convention', () => {
    expect(cardSrc).toMatch(/held-side-\{.*side.*toLowerCase\(\)\}/);
    expect(cardSrc).toMatch(/\.held-side-buy\s*\{\s*color:\s*var\(--btn-buy\)/);
    expect(cardSrc).toMatch(/\.held-side-sell\s*\{\s*color:\s*var\(--btn-sell\)/);
  });

  test('an InfoHint explains what "held" means', () => {
    expect(cardSrc).toMatch(/import InfoHint from/);
    expect(cardSrc).toMatch(/<InfoHint\s+text=/);
  });

  test('.held-count no longer floats in the middle via justify-content:space-between on 3 children', () => {
    const head = cardSrc.match(/\.held-head\s*\{[^}]*\}/)?.[0] ?? '';
    expect(head).not.toMatch(/justify-content:\s*space-between/);
    const count = cardSrc.match(/\.held-count\s*\{[^}]*\}/)?.[0] ?? '';
    expect(count).not.toBe('');
  });
});
