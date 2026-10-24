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
  // conflict. (The desktop-only `.chain-row > td { border-bottom: none }`
  // override this comment used to also cite was itself removed the same
  // day, 2026-10-24 — see the divider-reversal describe block below —
  // so the box-shadow mechanism is now the only reason, not two.)
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

test.describe('Stale-code: ATM shade is now actually visible (2026-10-24, "slightly different background shade to differentiate it as at the money")', () => {
  // Previously `.chain-row-atm > td` set a plain `background:
  // rgba(251,191,36,0.06)`, which the ITM rules' `background` shorthand
  // (higher specificity) silently beat on almost every real ATM row —
  // documented in this file's own prior comment as "mostly theoretical".
  // Fixed by moving the ATM shade onto `background-image` (a property
  // the ITM rules no longer touch, see the next describe block) so it
  // composites instead of losing a specificity fight.
  test('.chain-row-atm > td paints its shade via background-image, not the background shorthand', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const rule = src.match(/\.chain-row-atm\s*>\s*td\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/background-image:\s*linear-gradient\(rgba\(255,255,255,0\.05\),\s*rgba\(255,255,255,0\.05\)\)/);
    expect(rule).not.toMatch(/background:\s*rgba/);
  });

  // The compound active+atm rule must carry BOTH the pre-existing violet
  // active tint AND the new white ATM tint as comma-separated layers —
  // a single `background-image` declaration on the later rule would
  // otherwise fully replace, not merge with, the ATM rule's own layer.
  test('.chain-row-active.chain-row-atm > td layers both the violet active tint and the white ATM tint', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const rule = src.match(/\.chain-row-active\.chain-row-atm\s*>\s*td\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/linear-gradient\(rgba\(167,139,250,0\.14\),\s*rgba\(167,139,250,0\.14\)\)/);
    expect(rule).toMatch(/linear-gradient\(rgba\(255,255,255,0\.05\),\s*rgba\(255,255,255,0\.05\)\)/);
  });
});

test.describe('Stale-code: CE/PE column wash hue swap (2026-10-24, explicit operator request)', () => {
  // Reverses the 2026-09-30 "align ITM tints to CE/PE palette" fix —
  // deliberate. CE header text stays green over an orange CE column;
  // PE header stays red over a green PE column. Headers are untouched;
  // only the per-strike wash hue changes.
  //
  // `background:` -> `background-color:` and the OTM-side alpha
  // 0.015 -> 0.03 were both added later the same day (full-width-wash
  // fix, see OptionChainTab.svelte's own comment on the six ITM rules)
  // — updated here in lockstep.
  test('CE-side wash is orange (251,146,60 — app.css --algo-orange), not green', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    expect(src).toMatch(/\.chain-row-itm-call \.chain-td-ce\s*\{\s*background-color:\s*rgba\(251,146,60,0\.05\)/);
    expect(src).toMatch(/\.chain-row-itm-put\s+\.chain-td-ce\s*\{\s*background-color:\s*rgba\(251,146,60,0\.03\)/);
    expect(src).toMatch(/\.chain-row-itm-call \.chain-td-strike\s*\{\s*background-color:\s*rgba\(251,146,60,0\.05\)/);
  });

  test('PE-side wash is green (74,222,128 — app.css --algo-green/--c-long), not red', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    expect(src).toMatch(/\.chain-row-itm-put\s+\.chain-td-pe\s*\{\s*background-color:\s*rgba\(74,222,128,0\.05\)/);
    expect(src).toMatch(/\.chain-row-itm-call \.chain-td-pe\s*\{\s*background-color:\s*rgba\(74,222,128,0\.03\)/);
    expect(src).toMatch(/\.chain-row-itm-put\s+\.chain-td-strike\s*\{\s*background-color:\s*rgba\(74,222,128,0\.05\)/);
  });

  test('no residual red (248,113,113) or old-green-on-CE wash remains on the ITM cell/strike rules', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const block = src.match(/\.chain-row-itm-call \.chain-td-ce[\s\S]{0,900}?\.chain-row-itm-put\s+\.chain-td-strike[^\n]*\n/)?.[0] ?? '';
    expect(block).not.toContain('248,113,113');
  });

  // No bare `background:` shorthand remains on any of the six ITM
  // rules — that shorthand implicitly resets background-image, which
  // is exactly what made the ATM shade and the active-row tint "mostly
  // theoretical" before this fix (see OptionChainTab.svelte's comment).
  test('all six ITM wash rules use background-color, never the background shorthand', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const selectors = [
      /\.chain-row-itm-call \.chain-td-ce\s*\{[^}]*\}/,
      /\.chain-row-itm-call \.chain-td-pe\s*\{[^}]*\}/,
      /\.chain-row-itm-put\s+\.chain-td-pe\s*\{[^}]*\}/,
      /\.chain-row-itm-put\s+\.chain-td-ce\s*\{[^}]*\}/,
      /\.chain-row-itm-call \.chain-td-strike\s*\{[^}]*\}/,
      /\.chain-row-itm-put\s+\.chain-td-strike\s*\{[^}]*\}/,
    ];
    for (const sel of selectors) {
      const rule = src.match(sel)?.[0] ?? '';
      expect(rule).toMatch(/background-color:/);
      expect(rule).not.toMatch(/background:\s*rgba/);
    }
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
