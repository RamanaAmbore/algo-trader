/**
 * chain_atm_softening_and_leg_badge.spec.js
 *
 * Guards three 2026-09-29 operator-requested chain-grid refinements,
 * source-grep only (basket-leg badge needs live bid/ask quotes to
 * populate, which aren't available in a market-closed CI run — the
 * staging logic itself is covered here structurally):
 *
 * 1. ATM row decoration softened — was `rgba(251,191,36,0.55)` top+bottom
 *    borders ("overpowering underline"), now `0.32`.
 * 2. Per-strike lot-detail badge (`.chain-leg-badge`) shows a staged
 *    leg's lot count in the CE cell (left) for calls and the PE cell
 *    (right) for puts — reusing the strike/optType fields stamped onto
 *    each basket leg by `addOptionToBasket`, no tradingsymbol re-parsing.
 * 3. Template-attachment indicator (`.chain-leg-badge-tmpl`) — the badge
 *    gets an amber ring + dot when a non-None template is currently
 *    armed, wired from SymbolPanel's own `_selectedTemplate`/
 *    `_shellUsingNone` (not a duplicate template-catalog lookup).
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const CHAIN_TAB_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/order/OptionChainTab.svelte',
);
const SYMBOL_PANEL_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/SymbolPanel.svelte',
);

test.describe('Stale-code: ATM row decoration softened', () => {
  test('.chain-row-atm border alpha reduced from 0.55 to 0.18 (softened twice, 2026-09-29), no stronger value remains', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const rule = src.match(/\.chain-row-atm\s*>\s*td\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/border-top:\s*1px solid rgba\(251,191,36,0\.18\)/);
    expect(rule).toMatch(/border-bottom:\s*1px solid rgba\(251,191,36,0\.18\)/);
    expect(rule).not.toContain('0.55');
  });
});

test.describe('Stale-code: per-strike lot-detail badge wiring', () => {
  test('_basketLegByKey map is built from staged legs, keyed by strike:optType', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    expect(src).toContain('_basketLegByKey');
    expect(src).toMatch(/map\.set\(`\$\{b\.strike\}:\$\{b\.optType\}`, b\)/);
  });

  test('addOptionToBasket stamps strike/optType onto new legs for badge lookup', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const pushBlock = src.match(/_pushToBasket\(\{[\s\S]{0,700}?\}\);\s*\n\s*basketError = ''; _flashToast\(_quickKeyOpt/)?.[0] ?? '';
    expect(pushBlock).toContain('strike, optType');
  });

  test('CE badge renders in the CE cell (left), PE badge in the PE cell (right)', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    // Both CE badge instances (ATM + non-ATM row) reference ceLeg inside
    // a chain-td-ce ancestor context — checked structurally via the
    // addOptionToBasket('CE', ...) anchor each badge sits right after.
    const ceOccurrences = [...src.matchAll(/addOptionToBasket\(k, 'CE', 'short'\)[\s\S]{0,200}?\{#if ceLeg\}/g)];
    expect(ceOccurrences.length).toBe(2); // ATM row + non-ATM row
    const peOccurrences = [...src.matchAll(/addOptionToBasket\(k, 'PE', 'short'\)[\s\S]{0,200}?\{#if peLeg\}/g)];
    expect(peOccurrences.length).toBe(2);
  });
});

test.describe('Stale-code: template-attachment indicator on the leg badge', () => {
  test('OptionChainTab accepts templateName/templateIsNone and derives tmplAttached', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    expect(src).toContain('templateName');
    expect(src).toContain('templateIsNone');
    expect(src).toMatch(/tmplAttached\s*=\s*!templateIsNone\s*&&\s*!!templateName/);
    expect(src).toContain('chain-leg-badge-tmpl');
  });

  test('SymbolPanel passes its own resolved template (not a duplicate lookup) down to OptionChainTab', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(src).toMatch(/templateName=\{_selectedTemplate\?\.name \|\| _selectedTemplate\?\.slug \|\| ''\}/);
    expect(src).toMatch(/templateIsNone=\{_shellUsingNone\}/);
  });
});
