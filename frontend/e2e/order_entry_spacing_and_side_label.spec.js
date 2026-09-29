/**
 * order_entry_spacing_and_side_label.spec.js
 *
 * Guards three 2026-09-29 operator-requested fixes, source-grep only:
 *
 * 1. The footer's side-preview label ("Pick side" / "BUY" / "SELL")
 *    still read as a second clickable button even after being converted
 *    from <button> to <span> — its pill chrome (border, background,
 *    min-width) remained. Stripped to plain colored text so exactly one
 *    thing on the row (Submit) looks clickable.
 * 2. CHASE label sat flush against the L/M/H picker (.oes-tabs has
 *    gap:0, no spacing owned by either child). Added a gap wrapper.
 * 3. The BUY/SELL toggle (SideToggle.svelte) was one merged segmented-
 *    control shape with no space between the two options. Split into
 *    two independently-bordered buttons with a real gap.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const SYMBOL_PANEL_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/SymbolPanel.svelte',
);
const SIDE_TOGGLE_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/order/SideToggle.svelte',
);

test.describe('Stale-code: footer side label is plain text, not a pill', () => {
  test('.oes-footer-side-btn-single has no border/background/min-width', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    const rule = src.match(/\.oes-footer-side-btn-single\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toContain('border: none');
    expect(rule).not.toMatch(/min-width/);
    expect(rule).not.toMatch(/border-radius/);
  });

  test('on-none/on-buy/on-sell variants carry color only, no border-color', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    const noneRule = src.match(/\.oes-footer-side-btn-single\.on-none\s*\{[^}]*\}/)?.[0] ?? '';
    const buyRule = src.match(/\.oes-footer-side-btn-single\.on-buy\s*\{[^}]*\}/)?.[0] ?? '';
    const sellRule = src.match(/\.oes-footer-side-btn-single\.on-sell\s*\{[^}]*\}/)?.[0] ?? '';
    expect(noneRule).not.toMatch(/border-color/);
    expect(buyRule).not.toMatch(/border-color/);
    expect(sellRule).not.toMatch(/border-color/);
  });
});

test.describe('Stale-code: CHASE label has a gap before the L/M/H picker', () => {
  test('.oes-chase-agg-gap wrapper with margin-left exists and wraps ChaseAggPicker', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(src).toMatch(/<span class="oes-chase-agg-gap">\s*<ChaseAggPicker/);
    const rule = src.match(/\.oes-chase-agg-gap\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/margin-left:\s*0\.4rem/);
  });
});

test.describe('Stale-code: BUY/SELL toggle buttons have a real gap', () => {
  test('.ot-side-toggle-compact uses gap, not a merged overflow:hidden shape', () => {
    const src = readFileSync(SIDE_TOGGLE_PATH, 'utf8');
    const containerRule = src.match(/\.ot-side-toggle-compact\s*\{[^}]*\}/)?.[0] ?? '';
    expect(containerRule).toMatch(/gap:\s*0\.4rem/);
    // Real CSS declaration only (semicolon-terminated) — the explanatory
    // comment in this block mentions "overflow:hidden" as prose describing
    // the OLD design, which would false-positive a bare substring match.
    expect(containerRule).not.toMatch(/overflow:\s*hidden\s*;/);

    const btnRule = src.match(/\.ot-side-toggle-compact \.ot-side-btn\s*\{[^}]*\}/)?.[0] ?? '';
    expect(btnRule).toMatch(/border:\s*1px solid/);
    expect(btnRule).toMatch(/border-radius/);
  });
});
