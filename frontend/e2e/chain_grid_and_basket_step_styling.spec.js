/**
 * chain_grid_and_basket_step_styling.spec.js
 *
 * Guards two 2026-09-29 cosmetic fixes, source-grep only (no live render
 * needed for pure CSS-property regression guards):
 *
 * 1. Basket pill lot +/- steppers (`SymbolPanel.svelte` `.oes-basket-pill-step`,
 *    shared by both the Ticket-tab basket mode and the Chain tab — Chain is
 *    always basket mode) were borderless/transparent at rest and didn't read
 *    as buttons. Now carry the same amber-chip background/border treatment
 *    as `QtyInput.svelte`'s `.ot-lots-step` (the ticket form's own lot
 *    stepper) — reused token choice, not a new palette.
 * 2. `OptionChainTab.svelte`'s strike-grid table had row dividers but no
 *    column dividers between CE | Strike | PE. Added subtle (6% white)
 *    borders on the Strike column, matching the row dividers' whisper-quiet
 *    weight, so the grid reads as a true grid without extra visual noise.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const SYMBOL_PANEL_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/SymbolPanel.svelte',
);
const CHAIN_TAB_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/order/OptionChainTab.svelte',
);

test.describe('Stale-code: basket pill stepper has real button chrome', () => {
  test('.oes-basket-pill-step has a non-transparent background and a border', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    const rule = src.match(/\.oes-basket-pill-step\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).not.toContain('background: transparent');
    expect(rule).toMatch(/background:\s*rgba\(251,\s*191,\s*36,/);
    expect(rule).toMatch(/border:\s*1px solid rgba\(251,\s*191,\s*36,/);
    expect(rule).toContain('border-radius');
  });
});

test.describe('Stale-code: chain grid has subtle column dividers', () => {
  test('Strike column (header + body) carries a subtle left/right border', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');

    // .chain-th-strike's left/right border was folded into box-shadow:
    // inset (2026-09-30, commit 905c0c08 — sticky <th> + border-collapse
    // repaint-bug fix) alongside its bottom edge; still a border,
    // functionally, just expressed as box-shadow so it survives the
    // same repaint that used to drop border-bottom.
    const thRule = src.match(/\.chain-th-strike\s*\{[^}]*\}/)?.[0] ?? '';
    expect(thRule).toMatch(/box-shadow:[^;]*inset 1px 0 0 rgba\(255,\s*255,\s*255,\s*0\.03\)/);
    expect(thRule).toMatch(/box-shadow:[^;]*inset -1px 0 0 rgba\(255,\s*255,\s*255,\s*0\.03\)/);

    const tdRule = src.match(/\.chain-row\s*>\s*td\.chain-td-strike\s*\{[^}]*\}/)?.[0] ?? '';
    expect(tdRule).toMatch(/border-left:\s*1px solid rgba\(255,\s*255,\s*255,\s*0\.03\)/);
    expect(tdRule).toMatch(/border-right:\s*1px solid rgba\(255,\s*255,\s*255,\s*0\.03\)/);
  });

  test('Row dividers remain subtle (halved again 2026-09-29, "very very subtle" pass)', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const rowRule = src.match(/\.chain-row\s*>\s*td\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rowRule).toMatch(/border-bottom:\s*1px solid rgba\(255,\s*255,\s*255,\s*0\.025\)/);
  });
});
