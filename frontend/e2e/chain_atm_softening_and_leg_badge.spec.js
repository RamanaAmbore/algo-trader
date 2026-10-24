/**
 * chain_atm_softening_and_leg_badge.spec.js
 *
 * Guards three 2026-09-29 operator-requested chain-grid refinements,
 * source-grep only (basket-leg badge needs live bid/ask quotes to
 * populate, which aren't available in a market-closed CI run — the
 * staging logic itself is covered here structurally):
 *
 * 1. ATM row decoration softened — was `rgba(251,191,36,0.55)` top+bottom
 *    borders ("overpowering underline"), now `0.32`. SUPERSEDED 2026-10-24
 *    (see the describe block below) — the marker is now a box-shadow, not
 *    a border, and deliberately carries BOTH a top and a bottom edge
 *    again, reversing the 2026-09-30 "bottom-only" decision.
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
  test('.chain-row-atm box-shadow alpha stays at the softened 0.18, no stronger value remains', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const rule = src.match(/\.chain-row-atm\s*>\s*td\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/box-shadow:\s*inset 0 1px 0 rgba\(251,191,36,0\.18\),\s*inset 0 -1px 0 rgba\(251,191,36,0\.18\)/);
    expect(rule).not.toContain('0.55');
  });

  // 2026-10-24 (explicit operator request): a top-AND-bottom marker on
  // the ATM row, reversing the 2026-09-30 "bottom-only" decision noted
  // above. Implemented as `box-shadow`, not `border-top`/`border-bottom`
  // — `.chain-grid` is `border-collapse: collapse`, so a literal
  // `border-top` would conflict at the shared edge with the row ABOVE's
  // own `border-bottom` (`.chain-row > td`, same width/specificity);
  // box-shadow isn't part of the table border model and never has that
  // conflict. It also isn't reset by the `@media (min-width: 640px)`
  // override further down that sets `.chain-row > td { border-bottom:
  // none }` for desktop — that override only touches `border-bottom`,
  // so a literal `border-bottom`-based marker here would have silently
  // disappeared on desktop while still showing on mobile.
  test('.chain-row-atm has no literal border-top/border-bottom — box-shadow carries both edges instead', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const rule = src.match(/\.chain-row-atm\s*>\s*td\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).not.toMatch(/border-top/);
    expect(rule).not.toMatch(/border-bottom/);
  });

  // .chain-row-active > td sets its own `box-shadow` (same 0,1,1
  // specificity, later in source) which would otherwise fully replace
  // — not merge with — .chain-row-atm > td's box-shadow when a row is
  // both ATM and active, silently dropping the amber top edge. The
  // compound selector must carry both signals explicitly.
  test('.chain-row-active.chain-row-atm > td keeps the amber top edge alongside the violet active bottom edge', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const rule = src.match(/\.chain-row-active\.chain-row-atm\s*>\s*td\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/box-shadow:\s*inset 0 1px 0 rgba\(251,191,36,0\.18\),\s*inset 0 -1px 0 rgba\(167,139,250,0\.55\)/);
  });
});

test.describe('Stale-code: CE/PE column wash hue swap (2026-10-24, explicit operator request)', () => {
  // Reverses the 2026-09-30 "align ITM tints to CE/PE palette" fix —
  // deliberate. CE header text stays green over an orange CE column;
  // PE header stays red over a green PE column. Headers are untouched;
  // only the per-strike wash hue changes.
  test('CE-side wash is orange (251,146,60 — app.css --algo-orange), not green', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    expect(src).toMatch(/\.chain-row-itm-call \.chain-td-ce\s*\{\s*background:\s*rgba\(251,146,60,0\.05\)/);
    expect(src).toMatch(/\.chain-row-itm-put\s+\.chain-td-ce\s*\{\s*background:\s*rgba\(251,146,60,0\.015\)/);
    expect(src).toMatch(/\.chain-row-itm-call \.chain-td-strike\s*\{\s*background:\s*rgba\(251,146,60,0\.05\)/);
  });

  test('PE-side wash is green (74,222,128 — app.css --algo-green/--c-long), not red', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    expect(src).toMatch(/\.chain-row-itm-put\s+\.chain-td-pe\s*\{\s*background:\s*rgba\(74,222,128,0\.05\)/);
    expect(src).toMatch(/\.chain-row-itm-call \.chain-td-pe\s*\{\s*background:\s*rgba\(74,222,128,0\.015\)/);
    expect(src).toMatch(/\.chain-row-itm-put\s+\.chain-td-strike\s*\{\s*background:\s*rgba\(74,222,128,0\.05\)/);
  });

  test('no residual red (248,113,113) or old-green-on-CE wash remains on the ITM cell/strike rules', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const block = src.match(/\.chain-row-itm-call \.chain-td-ce[\s\S]{0,900}?\.chain-row-itm-put\s+\.chain-td-strike[^\n]*\n/)?.[0] ?? '';
    expect(block).not.toContain('248,113,113');
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
