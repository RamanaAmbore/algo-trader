// 2026-10 P1 card-chrome audit — border-radius + shadow + popup-chrome drift
// across the core trading-grid surfaces (MarketPulse, NavBreakdown, NavTab,
// PositionStrip). Fix converged every card wrapper in scope onto app.css's
// canonical .bucket-card recipe (var(--algo-card-border), 6px radius, the
// 0.35/0.06 shadow pair) and every popup onto .algo-modal's recipe
// (var(--card-bg-gradient), amber 0.55 border, 6px radius). Source-level
// guard, same pattern as held_orders_card_styling.spec.js — no browser
// needed, just regex assertions against the raw file text.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const appCss = readFileSync(new URL('../src/app.css', import.meta.url).pathname, 'utf8');
const pulseSrc = readFileSync(new URL('../src/lib/MarketPulse.svelte', import.meta.url).pathname, 'utf8');
const navBdSrc = readFileSync(new URL('../src/lib/NavBreakdown.svelte', import.meta.url).pathname, 'utf8');
const navTabSrc = readFileSync(new URL('../src/lib/NavTab.svelte', import.meta.url).pathname, 'utf8');
const posStripSrc = readFileSync(new URL('../src/lib/PositionStrip.svelte', import.meta.url).pathname, 'utf8');

// Anchored to the START of a line so a descendant selector sharing the same
// trailing class name (e.g. `.mp-col > .mp-bucket-wrap {`) never wins the
// match ahead of the real top-level rule. `className` is a plain selector
// like '.mp-bucket-wrap' — an optional `:global(...)` wrapper (used by
// MarketPulse.svelte's shared-popup classes) is matched automatically so
// callers don't need to hand-escape it themselves.
function rule(src, className) {
  const escaped = className.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(`^\\s*(?::global\\()?${escaped}\\)?\\s*\\{[^}]*\\}`, 'm');
  return src.match(re)?.[0] ?? '';
}

// Strips /* ... */ comments so "must NOT contain X" assertions don't false-
// positive on a WHY-comment that legitimately mentions the old broken value
// (e.g. explaining what --border-color used to be) rather than a live decl.
function stripComments(ruleStr) {
  return ruleStr.replace(/\/\*[\s\S]*?\*\//g, '');
}

// rgba() comma-spacing is an inconsistent, pre-existing convention across
// these files (app.css spaces after commas, MarketPulse.svelte mostly
// doesn't) — not a value difference worth "fixing" as part of this radius/
// shadow audit. Normalize spacing before comparing so the test asserts on
// the actual color+alpha, not incidental whitespace style.
function normSpace(s) {
  return s.replace(/,\s*/g, ',');
}

test.describe('card chrome — border/radius/shadow converge on .bucket-card', () => {
  const bucketCard = rule(appCss, '.bucket-card');
  const mpBucketWrap = rule(pulseSrc, '.mp-bucket-wrap');
  const navBdWrap = rule(navBdSrc, '.nav-bd-wrap');

  test('.bucket-card (app.css) uses the tokenized border + 6px radius', () => {
    expect(bucketCard).toMatch(/border:\s*1\.5px solid var\(--algo-card-border\)/);
    expect(bucketCard).toMatch(/border-radius:\s*6px/);
    expect(bucketCard).toMatch(/0\.35\)/);
    expect(bucketCard).toMatch(/0\.06\)/);
  });

  test('.mp-bucket-wrap (MarketPulse) matches .bucket-card border token + radius + shadow', () => {
    expect(mpBucketWrap).toMatch(/border:\s*1\.5px solid var\(--algo-card-border\)/);
    expect(mpBucketWrap).toMatch(/border-radius:\s*6px/);
    expect(mpBucketWrap).toMatch(/0\.35\)/);
    expect(mpBucketWrap).toMatch(/0\.06\)/);
    // No stale raw-literal border left behind.
    expect(mpBucketWrap).not.toMatch(/rgba\(255,\s*255,\s*255,\s*0\.10\)/);
  });

  test('.nav-bd-wrap (NavBreakdown) matches .bucket-card border token + radius + shadow', () => {
    expect(navBdWrap).toMatch(/border:\s*1\.5px solid var\(--algo-card-border\)/);
    expect(navBdWrap).toMatch(/border-radius:\s*6px/);
    expect(navBdWrap).toMatch(/0\.35\)/);
    expect(navBdWrap).toMatch(/0\.06\)/);
    expect(navBdWrap).not.toMatch(/border-radius:\s*4px/);
  });
});

test.describe('NavTab — no inner card frame fighting the chart-card tab switch', () => {
  test('.nav-tab-wrap no longer carries its own border/background', () => {
    const wrap = rule(navTabSrc, '.nav-tab-wrap');
    expect(wrap).not.toMatch(/border:/);
    expect(wrap).not.toMatch(/background:/);
    // Position/padding are load-bearing (chip anchor + content inset) —
    // must survive the chrome removal.
    expect(wrap).toMatch(/position:\s*relative/);
    expect(wrap).toMatch(/padding:/);
  });
});

// Parse .algo-modal's own values at module scope so every test below
// guards against future drift in the canonical recipe too, not just a
// hardcoded snapshot.
const algoModal = rule(appCss, '.algo-modal');
const borderMatch = algoModal.match(/border:\s*1px solid (rgba\([^)]+\))/);
const radiusMatch = algoModal.match(/border-radius:\s*(\d+px)/);
const algoModalBorder = borderMatch?.[1] ?? '';
const algoModalRadius = radiusMatch?.[1] ?? '';

test.describe('popup chrome — converges on .algo-modal\'s real recipe', () => {
  test('.algo-modal canonical values are the ones this suite expects (0.55 amber, 6px)', () => {
    expect(borderMatch, '.algo-modal border not found in app.css').toBeTruthy();
    expect(radiusMatch, '.algo-modal border-radius not found in app.css').toBeTruthy();
    expect(algoModalBorder).toBe('rgba(251, 191, 36, 0.55)');
    expect(algoModalRadius).toBe('6px');
  });

  test('.ps-breakdown-panel (PositionStrip) actually matches .algo-modal border/radius, not just its own comment', () => {
    const panel = rule(posStripSrc, '.ps-breakdown-panel');
    expect(panel).toContain(algoModalBorder);
    expect(panel).toContain(`border-radius: ${algoModalRadius}`);
    // The old undefined --border-color indirection must be gone from the
    // live declaration (the WHY-comment is allowed to still mention it —
    // strip comments before asserting).
    expect(stripComments(panel)).not.toMatch(/var\(--border-color/);
  });

  test('.search-modal (MarketPulse) uses var(--card-bg-gradient) and the amber-0.55 border', () => {
    const modal = rule(pulseSrc, '.search-modal');
    expect(modal).toContain('var(--card-bg-gradient)');
    expect(normSpace(modal)).toContain(normSpace(algoModalBorder));
    expect(modal).not.toMatch(/linear-gradient\(180deg, #0c1830/);
  });

  test('.ctx-menu (MarketPulse) border-alpha + radius converge on .algo-modal; background stays distinct', () => {
    const menu = rule(pulseSrc, '.ctx-menu');
    expect(normSpace(menu)).toContain(normSpace(algoModalBorder));
    expect(menu).toContain(`border-radius: ${algoModalRadius}`);
    // Deliberately-kept distinct dark background for the context-menu
    // interaction pattern (not converged to --card-bg-gradient).
    expect(menu).toMatch(/background:\s*rgba\(10,\s*22,\s*40,\s*0\.97\)/);
  });
});

test.describe('close-button border tokenized onto --algo-red-border-soft', () => {
  test('MarketPulse .search-close and PositionStrip .ps-bd-close no longer hardcode the 0.35 literal', () => {
    const searchClose = rule(pulseSrc, '.search-close');
    const psClose = rule(posStripSrc, '.ps-bd-close');
    expect(searchClose).toMatch(/var\(--algo-red-border-soft\)/);
    expect(psClose).toMatch(/var\(--algo-red-border-soft\)/);
    expect(searchClose).not.toMatch(/rgba\(248,\s*113,\s*113,\s*0\.35\)/);
    expect(psClose).not.toMatch(/rgba\(248,\s*113,\s*113,\s*0\.35\)/);
  });

  test('--algo-red-border-soft token exists in app.css', () => {
    expect(appCss).toMatch(/--algo-red-border-soft:\s*rgba\(248,\s*113,\s*113,\s*0\.30\)/);
  });
});
