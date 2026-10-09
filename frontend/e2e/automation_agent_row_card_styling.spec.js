// 2026-10 — operator ask: "make agents row background look similar to
// agent templates in automation." automation/+page.svelte's agent rows use
// the shared global .algo-status-card class, which app.css gives no
// background/border/radius of its own (only [data-status="..."] variants
// setting --st-fg/--st-bg/--st-border for the nested .algo-status-pill
// badge) — so rows rendered with no visible card chrome. Fix: a scoped
// .algo-status-card rule inside automation/+page.svelte's own <style>
// block, matching agent-templates/+page.svelte's .frag-row look exactly.
// Source-level guard, same pattern as held_orders_card_styling.spec.js.
//
// This is a REGRESSION GUARD: if a future edit "fixes" this globally in
// app.css instead of per-page, this test catches the drift by asserting
// the rule lives in automation/+page.svelte's own <style> block, and that
// app.css's base (non-attribute-selector) .algo-status-card rule still has
// no background/border of its own.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const pageSrc = readFileSync(
  new URL('../src/routes/(algo)/automation/+page.svelte', import.meta.url).pathname,
  'utf8'
);
const appCss = readFileSync(
  new URL('../src/app.css', import.meta.url).pathname,
  'utf8'
);

test.describe('automation agent rows — card chrome matches agent-templates .frag-row', () => {
  test('automation/+page.svelte has its own scoped .algo-status-card rule in <style>', () => {
    const styleBlock = pageSrc.match(/<style>([\s\S]*)<\/style>/)?.[1] ?? '';
    expect(styleBlock).toMatch(/\.algo-status-card\s*\{/);
  });

  test('the scoped rule matches agent-templates .frag-row gradient, with a deliberately bumped border alpha', () => {
    // 2026-10 follow-up: .frag-row's own 0.10 border read as near-
    // invisible against this dark gradient (operator: "rows ... can have
    // a subtle border around them") — bumped to var(--card-divider)
    // (0.20) for actual visibility. Gradient/radius/overflow still match
    // .frag-row exactly; only the border alpha is an intentional
    // divergence from the original "match exactly" goal.
    const styleBlock = pageSrc.match(/<style>([\s\S]*)<\/style>/)?.[1] ?? '';
    const rule = styleBlock.match(/\.algo-status-card\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/linear-gradient\(180deg,\s*#0f1729 0%,\s*#0a1020 100%\)/);
    expect(rule).toMatch(/border:\s*1px solid var\(--card-divider\)/);
    expect(rule).toMatch(/border-radius:\s*0\.3rem/);
    expect(rule).toMatch(/overflow:\s*hidden/);
  });

  test('the scoped rule has a hover border-color matching .frag-row:hover', () => {
    const styleBlock = pageSrc.match(/<style>([\s\S]*)<\/style>/)?.[1] ?? '';
    const hoverRule = styleBlock.match(/\.algo-status-card:hover\s*\{[^}]*\}/)?.[0] ?? '';
    expect(hoverRule).toMatch(/rgba\(251,\s*191,\s*36,\s*0\.25\)/);
  });

  test('the scoped rule does not set padding (inline style="padding: 0" on the element already handles it)', () => {
    const styleBlock = pageSrc.match(/<style>([\s\S]*)<\/style>/)?.[1] ?? '';
    const rule = styleBlock.match(/\.algo-status-card\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).not.toMatch(/padding/);
  });

  test('app.css base .algo-status-card rule (no [data-status] selector) still has no background/border of its own', () => {
    // Match a bare `.algo-status-card {...}` rule — NOT `.algo-status-card[data-status=...]`
    // and NOT `.algo-status-card-2x`. Negative lookahead excludes both.
    const baseRuleMatch = appCss.match(/\.algo-status-card(?![-\[])\s*\{[^}]*\}/);
    // app.css deliberately has NO bare base rule for .algo-status-card at all
    // (confirms this fix stayed scoped to automation/+page.svelte and app.css
    // was not touched to add one).
    expect(baseRuleMatch).toBeNull();
  });

  test('app.css [data-status] variants only set --st-fg/--st-bg/--st-border custom properties', () => {
    const variantRules = appCss.match(/\.algo-status-card\[data-status="[^"]+"\]\s*\{[^}]*\}/g) ?? [];
    expect(variantRules.length).toBeGreaterThan(0);
    for (const rule of variantRules) {
      expect(rule).toMatch(/--st-fg:/);
      expect(rule).toMatch(/--st-bg:/);
      expect(rule).toMatch(/--st-border:/);
      expect(rule).not.toMatch(/\bbackground:\s*(?!var\(--st)/);
    }
  });
});
