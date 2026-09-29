/**
 * chain_desktop_button_bg_and_font_size.spec.js
 *
 * Guards two more 2026-09-29 operator-requested chain-grid fixes:
 *
 * 1. "+ and - are not looking like button" — .chain-btn-buy/-sell used a
 *    hover-reveal design (transparent background at rest, filled only on
 *    hover), which read as plain colored text rather than a button.
 *    Added a visible background fill at rest too.
 * 2. "on desktop, the row borders in chain making is cluttered" +
 *    "increase the text size in chain for desktop" — an EARLIER attempt
 *    at both fixes was placed in a @media (min-width: 640px) block that
 *    sat BEFORE the base chain-row, chain-grid, chain-th and chain-cell
 *    rules it was meant to override, so it silently lost the cascade
 *    (same selector + specificity, source order decides, and the later
 *    base rule won). Moved to a second, later @media (min-width: 640px)
 *    block placed after all of those base rules. This test guards BOTH
 *    that the override values exist AND that they appear at a source
 *    position after their conflicting base rules — the actual root cause
 *    of the original bug, not just presence-of-CSS.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const CHAIN_TAB_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/order/OptionChainTab.svelte',
);

test.describe('Stale-code: chain buy/sell buttons have a visible background at rest', () => {
  test('.chain-btn-buy/-sell use a filled background at rest, not transparent', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const buyRule = src.match(/\.chain-btn-buy\s*\{[^}]*\}/)?.[0] ?? '';
    const sellRule = src.match(/\.chain-btn-sell\s*\{[^}]*\}/)?.[0] ?? '';
    expect(buyRule).toMatch(/background:\s*var\(--c-long-10\)/);
    expect(sellRule).toMatch(/background:\s*var\(--c-short-10\)/);
  });
});

test.describe('Stale-code: desktop row-border/font-size overrides actually win the cascade', () => {
  test('the override @media block appears AFTER the base .chain-row > td rule (source order)', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const baseRuleIdx = src.indexOf('.chain-row > td {');
    // The override block's distinguishing content.
    const overrideIdx = src.indexOf('border-bottom: none;');
    expect(baseRuleIdx).toBeGreaterThan(-1);
    expect(overrideIdx).toBeGreaterThan(-1);
    expect(overrideIdx).toBeGreaterThan(baseRuleIdx);
  });

  test('the override block is scoped to min-width:640px and sets border-bottom:none + larger font-sizes', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const overrideIdx = src.indexOf('border-bottom: none;');
    const surrounding = src.slice(Math.max(0, overrideIdx - 400), overrideIdx + 400);
    expect(surrounding).toMatch(/@media \(min-width:\s*640px\)/);
    expect(surrounding).toMatch(/\.chain-grid\s*\{\s*font-size:\s*0\.78rem/);
  });
});
