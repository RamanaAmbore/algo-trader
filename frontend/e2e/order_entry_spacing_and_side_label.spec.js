/**
 * order_entry_spacing_and_side_label.spec.js
 *
 * Guards three 2026-09-29 operator-requested fixes, source-grep only:
 *
 * 1. The footer's side-preview label ("Pick side" / "BUY" / "SELL") went
 *    through two rounds: first de-buttonized (span, no click handler,
 *    stripped pill chrome), then — per repeated operator feedback that it
 *    STILL read as a second control — removed entirely ("remove the text
 *    before order submit in order ticket"). Submit's own label already
 *    restates verb+side+qty, so nothing functional was lost. Guards full
 *    removal of the markup, the CSS, and the now-dead `_addCloseVerb`
 *    helper that only that markup ever called.
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

test.describe('Stale-code: footer side-preview label removed entirely', () => {
  test('.oes-footer-side-btn-single markup and CSS are fully gone from SymbolPanel', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(src).not.toContain('oes-footer-side-btn-single');
    // "Pick side" as a footer-row span's literal rendered text is gone;
    // an unrelated comment elsewhere describing the ticket body's own
    // (still-live) SideToggle placeholder state may legitimately still
    // use the phrase, so check the specific removed markup shape instead
    // of the bare phrase.
    expect(src).not.toContain('<span>Pick side</span>');
  });

  test('_addCloseVerb helper removed too (only caller was the removed label)', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(src).not.toContain('_addCloseVerb');
  });

  test('Submit button is still the only clickable control in the footer row', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(src).toContain('oes-common-submit');
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
