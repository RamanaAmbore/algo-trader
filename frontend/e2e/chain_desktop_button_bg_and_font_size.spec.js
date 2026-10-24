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
 *    block placed after all of those base rules.
 *
 *    REVERSED 2026-10-24 (explicit new operator request: "I want subtle
 *    horizontal border for each strike") — the desktop-only
 *    `border-bottom: none;` override this block used to carry is gone;
 *    the base `.chain-row > td` divider (`border-bottom: 1px solid
 *    rgba(255,255,255,0.025)`) now applies on every viewport, not just
 *    mobile. The `@media (min-width: 640px)` block still exists (for the
 *    row vertical-padding restore) and still sits after the base rule —
 *    this describe block now guards THAT ordering/scoping instead of the
 *    now-removed border-bottom:none string.
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

test.describe('Stale-code: desktop row-padding override still wins the cascade (post-2026-10-24 divider reversal)', () => {
  test('the override @media block appears AFTER the base .chain-row > td rule (source order)', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const baseRuleIdx = src.indexOf('.chain-row > td {');
    // The override block's distinguishing content — vertical padding
    // restore (the only thing left in this block after the 2026-10-24
    // divider reversal removed `border-bottom: none;`).
    const overrideIdx = src.indexOf('padding-top: 0.3rem;');
    expect(baseRuleIdx).toBeGreaterThan(-1);
    expect(overrideIdx).toBeGreaterThan(-1);
    expect(overrideIdx).toBeGreaterThan(baseRuleIdx);
  });

  test('the override block is scoped to min-width:640px and restores row padding, without stripping border-bottom', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const overrideIdx = src.indexOf('padding-top: 0.3rem;');
    const surrounding = src.slice(Math.max(0, overrideIdx - 600), overrideIdx + 200);
    expect(surrounding).toMatch(/@media \(min-width:\s*640px\)/);
    expect(surrounding).toMatch(/padding-bottom:\s*0\.3rem;/);
  });

  test('desktop no longer strips the row divider — no border-bottom:none remains anywhere in the file', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    expect(src).not.toMatch(/border-bottom:\s*none/);
  });

  test('the base row divider (border-bottom, 0.025 alpha) applies unconditionally, not gated to a max-width block', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const rule = src.match(/\.chain-row\s*>\s*td\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/border-bottom:\s*1px solid rgba\(255,\s*255,\s*255,\s*0\.025\)/);
  });
});
