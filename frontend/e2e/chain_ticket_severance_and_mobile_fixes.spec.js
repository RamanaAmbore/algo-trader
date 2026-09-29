/**
 * chain_ticket_severance_and_mobile_fixes.spec.js
 *
 * Source-scan guards for the 2026-09-29 batch of fixes:
 *
 *  1. Ticket/Chain template architectural severance — the operator's
 *     final, repeatedly-confirmed decision ("make sure order ticket is
 *     not wired to template"): the Ticket tab places/closes positions
 *     with NO template attach at all; templates only ever apply to the
 *     Chain tab's basket legs.
 *  2. Mobile chain-area height cap — operator reported the Templ row
 *     was invisible on mobile because .chain-grid-wrap's unbounded
 *     flex-grow starved every sibling below it (including the Templ
 *     row) of any box height. Fixed with a mobile max-height cap.
 *  3. Desktop chain row gap — removing the row border-bottom (an
 *     earlier declutter fix) left rows with zero vertical padding on
 *     desktop, reading as "very close". Fixed with restored padding.
 *  4. Submit button labels — lot size dropped from the Ticket-tab
 *     label; the Chain-tab basket label is now plain "Submit" instead
 *     of "Submit (N)".
 *  5. Chase indicator right-alignment — CHASE was only pushed to the
 *     right edge via the LTP pill's margin-left:auto; when LTP doesn't
 *     render (common on Chain tab, no single-symbol LTP while staging
 *     a multi-leg basket) CHASE lost its right anchor. Fixed with its
 *     own margin-left:auto.
 *  6. Chain +/- buttons — :active pressed-state styling so a tap/click
 *     reads as tactile feedback, not just a hover restatement.
 *
 * 2026-09-30 batch (Templ toggle relocation + CE/PE layout/palette):
 *  7. Templ toggle relocated from a shell-level SymbolPanel row into
 *     OptionChainTab's own expiry toolbar row; TemplateBar.svelte's
 *     primary control changed from a Default/None/named <Select>
 *     dropdown to a compact ON/OFF toggle pill (named-template picker
 *     moved into the expand panel).
 *  8. CE/PE header text-align flipped to match where the +/- buttons
 *     actually sit; header border-bottom darkened; chain font-size
 *     reset to the platform's normal --fs-md/--fs-sm scale (0.78rem
 *     override removed); +/- buttons get a solid rest-state border +
 *     one-tier-stronger background.
 *  9. Chain leg badges show a truncated template short-label suffix
 *     when a template is attached.
 * 10. app.css's global `.oes-common-chase-label` fallback realigned to
 *     match SymbolPanel.svelte's own scoped copy (color drift fix).
 *
 * All source-scan (no browser) — mirrors this session's established
 * pattern for guarding CSS/markup decisions that are cheap to verify
 * from source and expensive/flaky to verify live on every push.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const dir = path.resolve(import.meta.dirname ?? new URL('.', import.meta.url).pathname, '..');
const SYMBOL_PANEL = readFileSync(path.join(dir, 'src/lib/SymbolPanel.svelte'), 'utf8');
const ORDER_TICKET = readFileSync(path.join(dir, 'src/lib/order/OrderTicket.svelte'), 'utf8');
const CHAIN_TAB = readFileSync(path.join(dir, 'src/lib/order/OptionChainTab.svelte'), 'utf8');
const SUBMIT_HELPERS = readFileSync(path.join(dir, 'src/lib/order/orderTicketSubmit.js'), 'utf8');
const TEMPLATE_BAR = readFileSync(path.join(dir, 'src/lib/TemplateBar.svelte'), 'utf8');
const APP_CSS = readFileSync(path.join(dir, 'src/app.css'), 'utf8');
const CHART_WORKSPACE = readFileSync(path.join(dir, 'src/lib/ChartWorkspace.svelte'), 'utf8');

test.describe('Ticket/Chain template severance', () => {
  test('Templ toggle/note visibility gates only ever fire for the Chain tab, never Ticket', () => {
    // 2026-09-30 — the shell-level demo/live if-else-if branch that used
    // to render <TemplateBar> directly was replaced by two $derived
    // booleans (_showTemplateBar / _showDemoTplNote), both scoped to
    // `_activeTab === 'chain'` inside their own definitions, threaded
    // down into <OptionChainTab> as plain props. Assert the gates
    // themselves still only ever apply to the Chain tab.
    const showTemplateBarDef = SYMBOL_PANEL.match(/const _showTemplateBar = \$derived\([\s\S]{0,220}?\);/)?.[0] ?? '';
    const showDemoNoteDef = SYMBOL_PANEL.match(/const _showDemoTplNote = \$derived\([\s\S]{0,220}?\);/)?.[0] ?? '';
    expect(showTemplateBarDef, '_showTemplateBar definition').not.toBe('');
    expect(showDemoNoteDef, '_showDemoTplNote definition').not.toBe('');
    expect(showTemplateBarDef).toMatch(/_templates\.length > 0/);
    expect(showTemplateBarDef).toMatch(/action === 'open'/);
    expect(showDemoNoteDef).toMatch(/_isDemo/);
    expect(showDemoNoteDef).toMatch(/action === 'open'/);
    // Both gates are threaded into <OptionChainTab>, which only ever
    // mounts when `_activeTab === 'chain'` (its own enclosing {#if}).
    expect(SYMBOL_PANEL).toMatch(/showTemplateBar=\{_showTemplateBar\}/);
    expect(SYMBOL_PANEL).toMatch(/showDemoTplNote=\{_showDemoTplNote\}/);
    // The old inline demo-note markup + its dedicated CSS rule blocks
    // must be gone (a removal-note comment mentioning the class names
    // in prose is fine) — the note itself now renders inside
    // OptionChainTab.
    expect(SYMBOL_PANEL).not.toMatch(/\.oes-basket-tpl-row-demo\s*\{/);
    expect(SYMBOL_PANEL).not.toMatch(/\.oes-basket-tpl-demo-note\s*\{/);
    expect(SYMBOL_PANEL).not.toMatch(/class="oes-basket-tpl-row-demo"/);
  });

  test('Templ toggle mounts inside OptionChainTab, never SymbolPanel', () => {
    // <TemplateBar (the real Svelte component mount, tag-open) must
    // exist in OptionChainTab.svelte and be entirely absent from
    // SymbolPanel.svelte — only pass-through prop names (showTemplateBar,
    // _showTemplateBar, etc.) may reference the word "TemplateBar" there.
    expect(CHAIN_TAB).toMatch(/<TemplateBar/);
    expect(SYMBOL_PANEL).not.toContain('<TemplateBar');
    // SymbolPanel no longer imports the component directly.
    expect(SYMBOL_PANEL).not.toMatch(/import TemplateBar\s+from/);
  });

  test('OrderTicket mount no longer auto-selects a template', () => {
    const rawMountFn = ORDER_TICKET.match(/onMount\(\(\) => \{[\s\S]*?\n  \}\);/)?.[0] ?? '';
    expect(rawMountFn, 'onMount body').not.toBe('');
    // Strip comment-only lines — the removal is explained in a code
    // comment that itself mentions the old call names in prose.
    const mountFn = rawMountFn
      .split('\n')
      .filter((line) => !/^\s*(\/\/|\*)/.test(line))
      .join('\n');
    expect(mountFn, 'onMount executable body must not call _autoSelectTemplate()')
      .not.toMatch(/_autoSelectTemplate\(\)/);
    expect(mountFn, 'onMount executable body must not call loadOrderTemplates()')
      .not.toMatch(/loadOrderTemplates\(\)/);
  });

  test('OrderTicket submit payload always sends template_id: null (not just for close orders)', () => {
    expect(ORDER_TICKET).toMatch(/templateId:\s*null,/);
    // The old conditional form (`_isCloseOrder ? null : templateId`) must be gone.
    expect(ORDER_TICKET).not.toMatch(/templateId:\s*_isCloseOrder\s*\?\s*null\s*:\s*templateId/);
  });

  test('side-aware Default resolution survives the severance — still wired into the Chain-only TemplateBar', () => {
    // Replaces the old e2e/template_default_pill.spec.js (removed):
    // that spec drove the 4-scope Default-pill matrix through the
    // Ticket tab's BUY/SELL toggle + the old pill UI, both gone now
    // (Ticket has no template row; the pill toggle became a dropdown
    // in an earlier commit this session). The _sideAwareDefault
    // resolver itself is untouched by the severance — only where it
    // renders changed (Chain tab only) — so this is a structural
    // continuity check, not a behavioural regression test.
    expect(SYMBOL_PANEL).toMatch(/const _sideAwareDefault = \$derived\.by/);
    expect(SYMBOL_PANEL).toContain('sideAwareDefault={_sideAwareDefault}');
  });

  test('side-aware Default falls back to BUY when no side is known yet, without touching _modalSide itself (2026-09-30 fix)', () => {
    // Bug: a fresh/cold order entry starts with _modalSide === null
    // (deliberate — see _modalSide's own declaration comment, preserves
    // the SideToggle's neutral state and the margin-preflight
    // short-circuit). appliesToFor(null, sym) falls through every
    // BUY/SELL branch to 'both', and no template has
    // is_default=true AND applies_to='both' — so Templ rendered
    // disabled ("No default template configured for this side/type")
    // on every fresh order, for every symbol, until the operator
    // explicitly picked a side. Fix: the scope guess (NOT _modalSide
    // itself) falls back to 'BUY' when nothing else is known yet.
    const scopeBlock = SYMBOL_PANEL.match(/const _sideAwareDefault = \$derived\.by\([\s\S]{0,2000}?\}\);/)?.[0] ?? '';
    expect(scopeBlock, '_sideAwareDefault derivation block').not.toBe('');
    expect(scopeBlock).toMatch(/_focusedLeg\?\.side \|\| _modalSide \|\| 'BUY'/);
    // The auto-swap effect (re-resolves the shared template on a side
    // flip) must use the identical fallback for consistency — otherwise
    // the initial toggle state and the swap-on-flip state could disagree.
    const swapEffectBlock = SYMBOL_PANEL.match(/let _lastSideScope[\s\S]{0,1200}?_appliesToFor\(sideForScope, symForScope\)/)?.[0] ?? '';
    expect(swapEffectBlock, 'side-flip auto-swap effect block').not.toBe('');
    expect(swapEffectBlock).toMatch(/_focusedLeg\?\.side \|\| _modalSide \|\| 'BUY'/);
  });

  test('Chain tab still owns the shared templateId binding (severance is Ticket-only)', () => {
    expect(SYMBOL_PANEL).toContain('bind:templateId={_sharedTemplateId}');
    // Only one bind:templateId consumer should exist in the shell markup
    // (OptionChainTab) — OrderTicket's mount no longer receives it.
    const bindCount = (SYMBOL_PANEL.match(/bind:templateId=/g) || []).length;
    expect(bindCount, 'exactly one bind:templateId in SymbolPanel (Chain only)').toBe(1);
  });
});

test.describe('Mobile chain height cap + desktop row gap', () => {
  test('chain-grid-wrap has a mobile max-height so siblings below it (Templ row) get box height', () => {
    const mobileBlock = CHAIN_TAB.match(/@media \(max-width: 760px\) \{\s*\.chain-grid-wrap \{[\s\S]{0,80}/)?.[0] ?? '';
    expect(mobileBlock, 'mobile .chain-grid-wrap max-height rule').not.toBe('');
    expect(mobileBlock).toMatch(/max-height:\s*16rem/);
  });

  test('desktop chain rows have restored vertical padding after border-bottom removal', () => {
    const desktopBlock = CHAIN_TAB.match(/@media \(min-width: 640px\) \{\s*\.chain-row > td \{[\s\S]{0,900}?\n    \}/)?.[0] ?? '';
    expect(desktopBlock, 'desktop .chain-row > td override block').not.toBe('');
    expect(desktopBlock).toMatch(/border-bottom:\s*none/);
    expect(desktopBlock).toMatch(/padding-top:\s*0\.3rem/);
    expect(desktopBlock).toMatch(/padding-bottom:\s*0\.3rem/);
  });
});

test.describe('Submit button labels', () => {
  test('formatSubmitLabel drops lot size from Ticket-tab labels', () => {
    const fn = SUBMIT_HELPERS.match(/export function formatSubmitLabel[\s\S]*?\n\}/)?.[0] ?? '';
    expect(fn).not.toBe('');
    expect(fn, 'no qty/qtySuffix should be interpolated into the label anymore')
      .not.toMatch(/qtySuffix/);
  });

  test('formatSubmitLabel returns plain "Submit" for basket mode (Chain tab), not "Submit (N)"', () => {
    const fn = SUBMIT_HELPERS.match(/export function formatSubmitLabel[\s\S]*?\n\}/)?.[0] ?? '';
    expect(fn).toMatch(/if \(ctx\.basketCount > 0\) return 'Submit';/);
  });

  test('SymbolPanel common-action submit button also renders plain "Submit" for basket mode', () => {
    expect(SYMBOL_PANEL).not.toMatch(/Submit \(\$\{basketLegs\.length\}\)/);
    expect(SYMBOL_PANEL).not.toMatch(/`Submit \(\$\{basketLegs\.length\}\)`/);
  });

  // 2026-09-30: operator — "chain button to start with shows submit
  // suffixed by additional text. which should be removed." Root cause:
  // _submitLabel's Chain branch was keyed on basketLegs.length > 0, not
  // on the active tab, so an EMPTY Chain basket fell through to the
  // Ticket-style side/verb label. Fixed by gating on _activeTab directly.
  test('_submitLabel is unconditionally "Submit" on the Chain tab, even with an empty basket', () => {
    const fn = SYMBOL_PANEL.match(/const _submitLabel = \$derived\.by\(\(\) => \{[\s\S]*?\n  \}\);/)?.[0] ?? '';
    expect(fn, '_submitLabel derived').not.toBe('');
    expect(fn).toMatch(/if \(basketLegs\.length > 0 \|\| _activeTab === 'chain'\) return 'Submit';/);
  });

  // 2026-09-30: operator — "for order ticket the submit button should
  // not have Submit in the label." formatSubmitLabel's resolved-side
  // branches (cq===0 and cq!==0) no longer prefix "Submit · " — only
  // the side/verb text is returned. The bare-fallback (!ctx.side) case
  // still returns "Submit" since there's nothing else to show there.
  test('formatSubmitLabel drops the "Submit" prefix entirely for resolved-side (Ticket) labels', () => {
    const fn = SUBMIT_HELPERS.match(/export function formatSubmitLabel[\s\S]*?\n\}/)?.[0] ?? '';
    expect(fn).not.toBe('');
    expect(fn).toMatch(/if \(cq === 0\) return ctx\.side;/);
    expect(fn).toMatch(/return `\$\{verb\}\/\$\{ctx\.side\}`;/);
    expect(fn, 'no "Submit ·" prefix should remain in any resolved-side branch')
      .not.toMatch(/`Submit · /);
  });
});

test.describe('Chase indicator right-alignment', () => {
  test('CHASE label owns its own margin-left:auto, independent of the LTP pill', () => {
    const rule = SYMBOL_PANEL.match(/\.oes-common-chase-label \{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oes-common-chase-label rule').not.toBe('');
    expect(rule).toMatch(/margin-left:\s*auto/);
  });
});

test.describe('Chain +/- pressed-state feedback', () => {
  test('chain-btn-buy/-sell have :active rules distinct from :hover', () => {
    expect(CHAIN_TAB).toMatch(/\.chain-btn-buy:active\s*\{/);
    expect(CHAIN_TAB).toMatch(/\.chain-btn-sell:active\s*\{/);
    const buyActive = CHAIN_TAB.match(/\.chain-btn-buy:active\s*\{[\s\S]*?\}/)?.[0] ?? '';
    expect(buyActive, 'pressed state should visually differ from rest/hover (border/box-shadow highlight)')
      .toMatch(/box-shadow|border-color/);
  });
});

test.describe('CE/PE header alignment + palette normalization (2026-09-30)', () => {
  test('CE header text-align right, PE header text-align left — matches the +/- button side', () => {
    const ceRule = CHAIN_TAB.match(/\.chain-th-ce\s*\{[^}]*\}/)?.[0] ?? '';
    const peRule = CHAIN_TAB.match(/\.chain-th-pe\s*\{[^}]*\}/)?.[0] ?? '';
    expect(ceRule, '.chain-th-ce rule').not.toBe('');
    expect(peRule, '.chain-th-pe rule').not.toBe('');
    expect(ceRule).toMatch(/text-align:\s*right/);
    expect(peRule).toMatch(/text-align:\s*left/);
    // The row-content alignment (+/- buttons toward Strike column) must
    // stay untouched — a deliberate, explicitly-commented layout choice.
    expect(CHAIN_TAB).toMatch(/\.chain-cell-row-ce\s*\{\s*justify-content:\s*flex-end;\s*\}/);
    expect(CHAIN_TAB).toMatch(/\.chain-cell-row-pe\s*\{\s*justify-content:\s*flex-start;\s*\}/);
  });

  test('no font-size: 0.78rem declaration remains anywhere in the chain grid CSS', () => {
    expect(CHAIN_TAB).not.toMatch(/font-size:\s*0\.78rem/);
  });

  test('chain header cells have a visibly darker border-bottom (0.18 alpha)', () => {
    for (const sel of ['.chain-th-ce', '.chain-th-pe', '.chain-th-strike']) {
      const rule = CHAIN_TAB.match(new RegExp(`\\.${sel.slice(1)}\\s*\\{[^}]*\\}`))?.[0] ?? '';
      expect(rule, `${sel} rule`).not.toBe('');
      expect(rule, `${sel} border-bottom alpha`).toMatch(/border-bottom:\s*1px solid rgba\(255,255,255,0\.18\)/);
    }
  });

  test('chain-btn-buy/-sell rest state uses the -14 background tier and a solid (non-alpha) border', () => {
    const buyRule = CHAIN_TAB.match(/\.chain-btn-buy\s*\{[^}]*\}/)?.[0] ?? '';
    const sellRule = CHAIN_TAB.match(/\.chain-btn-sell\s*\{[^}]*\}/)?.[0] ?? '';
    expect(buyRule).toMatch(/background:\s*var\(--c-long-14\)/);
    expect(sellRule).toMatch(/background:\s*var\(--c-short-14\)/);
    // Solid border — references the base color token, not an alpha-tinted `-NN` variant.
    expect(buyRule).toMatch(/border-color:\s*var\(--c-long\)/);
    expect(buyRule).not.toMatch(/border-color:\s*var\(--c-long-\d/);
    expect(sellRule).toMatch(/border-color:\s*var\(--c-short\)/);
    expect(sellRule).not.toMatch(/border-color:\s*var\(--c-short-\d/);
  });

  // 2026-09-30: operator — "empty space below chain strikes, chain is not
  // fully using available space". flex: 0 1 auto (content-only sizing) was
  // reversed to flex: 1 1 auto now that the Templ toggle it was protecting
  // has moved above the grid (into the expiry row) — nothing left below
  // the grid to starve, so it can grow to fill real leftover space again.
  test('mobile .chain-grid-wrap shrinks to content (flex: 0 1 auto), still capped at 16rem (2026-09-30 fix)', () => {
    // Was flex: 1 1 auto — grow:1 stretched the wrapper to consume all
    // leftover space in its flex-column parent up to the 16rem cap, even
    // when the actual strike-row content was shorter, leaving visible
    // empty space below the last row (operator: "why empty space below
    // chain on mobile"). Shrink-to-content instead.
    const mobileBlock = CHAIN_TAB.match(/@media \(max-width: 760px\) \{\s*\.chain-grid-wrap \{[\s\S]{0,1200}?\}/)?.[0] ?? '';
    expect(mobileBlock, 'mobile .chain-grid-wrap block').not.toBe('');
    expect(mobileBlock).toMatch(/max-height:\s*16rem/);
    // The real CSS declaration (not the explanatory comment prose above
    // it, which legitimately mentions the old value in passing).
    expect(mobileBlock).toMatch(/\n\s*flex:\s*0 1 auto;/);
    expect(mobileBlock).not.toMatch(/\n\s*flex:\s*1 1 auto;/);
  });
});

test.describe('Chain leg badge — template short-label suffix', () => {
  test('badge shows a truncated template short-label when tmplAttached', () => {
    expect(CHAIN_TAB).toMatch(/tmplShort\s*=\s*tmplAttached\s*\?\s*String\(templateName\)\.slice\(0,\s*6\)\s*:\s*''/);
    // All 4 leg-badge render sites (ATM CE/PE + non-ATM CE/PE) must
    // interpolate the short-label suffix, not just the bare lots count.
    const badgeMatches = CHAIN_TAB.match(/\{(?:ce|pe)Leg\.lots\}L\{tmplAttached \? ' · ' \+ tmplShort : ''\}/g) ?? [];
    expect(badgeMatches.length, 'expected 4 leg-badge occurrences (ATM CE/PE + non-ATM CE/PE)').toBe(4);
    // The tooltip's own template-name string construction is untouched.
    expect(CHAIN_TAB).toMatch(/tmplAttached \? ' · template: ' \+ templateName : ' · no template'/);
  });
});

test.describe('TemplateBar — single toggle button replaces the primary dropdown/pill (2026-09-30)', () => {
  test('primary control is one toggle button, not a <Select> or the retired two-button pill', () => {
    expect(TEMPLATE_BAR).toMatch(/class="oes-tpl-button"/);
    expect(TEMPLATE_BAR).toMatch(/class:active=\{_toggleOn\}/);
    // Retired two-button pill markup/classes must be gone.
    expect(TEMPLATE_BAR).not.toMatch(/class="oes-tpl-toggle"/);
    expect(TEMPLATE_BAR).not.toMatch(/oes-tpl-toggle-btn-on/);
    expect(TEMPLATE_BAR).not.toMatch(/oes-tpl-toggle-btn-off/);
    // The toggle markup itself (before the expand panel) must not use <Select>.
    const beforeExpandPanel = TEMPLATE_BAR.split('{#if _expanded}')[0] ?? '';
    expect(beforeExpandPanel).not.toMatch(/<Select/);
  });

  test('RE-WIRED (2026-09-30) — button calls the real onSelectDefault/onSelectNone handlers, not a debug-only local toggle', () => {
    // The temporary debug bypass (which isolated whether TemplateBar's
    // own logic was the display problem) was reverted after the actual
    // root cause was found and fixed: loadOrderTemplates() (templates.js)
    // permanently cached a transient fetch failure as an empty template
    // list. See that file's own test coverage
    // (frontend/src/lib/__tests__/data/templates.test.js) for the fix.
    const btn = TEMPLATE_BAR.match(/class="oes-tpl-button"[\s\S]{0,700}?<\/button>/)?.[0] ?? '';
    expect(btn, 'toggle button block').not.toBe('');
    expect(btn).toContain('onSelectDefault?.()');
    expect(btn).toContain('onSelectNone?.()');
    expect(btn).toMatch(/if\s*\(_toggleOn\)/);
    expect(btn).toMatch(/disabled=\{_templBtnDisabled\}/);
    expect(btn).not.toContain('_debugToggleClick');
    expect(TEMPLATE_BAR).not.toMatch(/let _debugOn/);
    // ON/active display state guards against a null _sharedTemplateId
    // reading as "armed" (the financial-risk-relevant fix) — must
    // require BOTH !shellUsingNone AND a concrete selectedTemplate.
    expect(TEMPLATE_BAR).toMatch(/_toggleOn = \$derived\(!shellUsingNone && !!selectedTemplate\)/);
  });

  test('a specific named-template picker exists inside the expand panel, scoped to nonNoneTemplates', () => {
    const expandPanel = TEMPLATE_BAR.match(/\{#if _expanded\}[\s\S]*$/)?.[0] ?? '';
    expect(expandPanel, 'expand panel block').not.toBe('');
    expect(expandPanel).toMatch(/oes-tpl-pick-specific/);
    expect(expandPanel).toMatch(/<Select/);
    expect(expandPanel).toMatch(/nonNoneTemplates\.map/);
    expect(expandPanel).toMatch(/onSelectTemplate\?\.\(Number\(v\)\)/);
  });

  test('all existing TP%/SL%/Wing/Trail-SL/Scale-ladder override inputs are untouched', () => {
    for (const bindable of ['tpOverride', 'slOverride', 'wingStrikeOffsetOverride', 'wingPremPctOverride', 'slTrailPctOverride', 'tpScalesJsonOverride']) {
      expect(TEMPLATE_BAR, `${bindable} still bind:value`).toMatch(new RegExp(`bind:value=\\{${bindable}\\}`));
    }
    // TP order type is a button-pair toggle (not bind:value) — unchanged shape.
    expect(TEMPLATE_BAR).toMatch(/tpOrderTypeOverride = 'LIMIT'/);
    expect(TEMPLATE_BAR).toMatch(/tpOrderTypeOverride = 'MARKET'/);
  });
});

test.describe('Color audit — .oes-common-chase-label drift fix (app.css)', () => {
  test("app.css global fallback matches SymbolPanel's scoped color values exactly", () => {
    const scopedRule = SYMBOL_PANEL.match(/\.oes-common-chase-label\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    const scopedOnRule = SYMBOL_PANEL.match(/\.oes-common-chase-label\.on\s*\{[^}]*\}/)?.[0] ?? '';
    expect(scopedRule, 'SymbolPanel scoped .oes-common-chase-label rule').not.toBe('');
    expect(scopedOnRule, 'SymbolPanel scoped .oes-common-chase-label.on rule').not.toBe('');
    expect(scopedRule).toMatch(/color:\s*var\(--algo-slate-muted\)/);
    expect(scopedOnRule).toMatch(/color:\s*var\(--c-action\)/);

    const globalRule = APP_CSS.match(/\.oes-common-chase-label\s*\{[\s\S]*?\n\}/)?.[0] ?? '';
    const globalOnRule = APP_CSS.match(/\.oes-common-chase-label\.on\s*\{[^}]*\}/)?.[0] ?? '';
    expect(globalRule, 'app.css global .oes-common-chase-label rule').not.toBe('');
    expect(globalOnRule, 'app.css global .oes-common-chase-label.on rule').not.toBe('');
    expect(globalRule).toMatch(/color:\s*var\(--algo-slate-muted\)/);
    expect(globalOnRule).toMatch(/color:\s*var\(--c-action\)/);
    // Old drifted values must be gone.
    expect(globalRule).not.toMatch(/color-mix\(in srgb, var\(--algo-slate\) 70%, transparent\)/);
    expect(globalOnRule).not.toMatch(/#fbbf24/);
  });
});

test.describe('Guard — no new order-placement call was added to any frontend submit path', () => {
  test('submitBasket still delegates to the existing placeBasket API helper, nothing new', () => {
    const fn = SYMBOL_PANEL.match(/async function submitBasket\(\) \{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(fn, 'submitBasket implementation').not.toBe('');
    expect(fn).toMatch(/await placeBasket\(groups\)/);
    // No direct broker/order-placement call bypassing the existing
    // placeBasket/placeTicketOrder API-layer helpers was introduced by
    // this batch of changes.
    for (const banned of ['apply_plan_live', 'broker.place_order', 'broker.place_gtt']) {
      expect(SYMBOL_PANEL, `SymbolPanel must not call ${banned}`).not.toContain(banned);
      expect(CHAIN_TAB, `OptionChainTab must not call ${banned}`).not.toContain(banned);
      expect(TEMPLATE_BAR, `TemplateBar must not call ${banned}`).not.toContain(banned);
    }
  });
});

// 2026-09-30: operator — "remove (l) from chain". The "(L)" no-live-depth
// indicator (shown next to a CE/PE quote when depthAvail is false, meaning
// the price shown is last-traded-price rather than live bid/ask) is removed
// entirely, all 4 occurrences (CE/PE x ATM/non-ATM rows) plus its CSS.
test.describe('Chain "(L)" no-depth indicator removed', () => {
  test('no chain-cell-no-depth markup or CSS remains in OptionChainTab.svelte', () => {
    expect(CHAIN_TAB).not.toContain('chain-cell-no-depth');
    expect(CHAIN_TAB).not.toMatch(/>\(L\)</);
    // depthAvail itself may still exist as a data-shape field (JSDoc type
    // comment) — only the rendered "(L)" indicator and its CSS are removed.
    expect(CHAIN_TAB).not.toMatch(/!ceQ\.depthAvail|!peQ\.depthAvail/);
  });
});

// 2026-09-30: operator — "you can remove rupee symbol from chart y label".
test.describe('Chart Y-axis label has no rupee symbol', () => {
  test('cw-yaxis-label text no longer prefixes priceFmt with ₹', () => {
    const label = CHART_WORKSPACE.match(/class="cw-yaxis-label"[\s\S]{0,300}?<\/text>/)?.[0] ?? '';
    expect(label, 'cw-yaxis-label <text> block').not.toBe('');
    expect(label).not.toContain('₹{priceFmt(tick.v)}');
    expect(label).toContain('{priceFmt(tick.v)}');
  });
});

// 2026-09-30: operator — "price width can be reduced by 20%".
test.describe('PRICE input narrowed 20%', () => {
  test('.ot-price-cell .ot-input is 7.2rem (was 9rem)', () => {
    expect(ORDER_TICKET).toMatch(/\.ot-price-cell \.ot-input \{ width:\s*7\.2rem;\s*\}/);
    expect(ORDER_TICKET).not.toMatch(/\.ot-price-cell \.ot-input \{ width:\s*9rem;\s*\}/);
  });
});

// 2026-09-30: operator — header DTE chip removal ("remove 15d from order
// ticket after symbol. I am reversing my decision.") — reverses the
// earlier same-day "make 15d common across every tab" change.
test.describe('Header DTE chip removed (reversed decision)', () => {
  test('SymbolPanel no longer computes or renders a header days-to-expiry chip', () => {
    expect(SYMBOL_PANEL).not.toMatch(/_headerDte/);
    expect(SYMBOL_PANEL).not.toMatch(/oes-header-dte/);
    expect(SYMBOL_PANEL).not.toMatch(/guessExpiryYmdFromSymbol/);
  });

  test('Chain tab keeps its own Expiry-row DTE chip, unaffected by the reversal', () => {
    expect(CHAIN_TAB).toMatch(/oct-expiry-dte/);
    expect(CHAIN_TAB).toMatch(/_daysToExpiry/);
  });
});

// 2026-09-30: RE-GATED after finding and fixing the actual root cause —
// loadOrderTemplates() (templates.js) permanently caching a transient
// fetch failure as an empty template list. A temporary gate bypass here
// (and a temporary internal-wiring bypass in TemplateBar.svelte, see the
// "RE-WIRED" test above) helped isolate that the bug was NOT in either
// this gate's own logic or TemplateBar's button logic — both now
// reverted to their real, permanent behavior.
test.describe('TemplateBar mount gate — real showDemoTplNote/showTemplateBar conditional restored', () => {
  test('<TemplateBar> is gated by {#if showDemoTplNote}{:else if showTemplateBar}, not unconditional', () => {
    const expiryIdx = CHAIN_TAB.indexOf('oct-toolbar-label">Expiry<');
    expect(expiryIdx, 'Expiry label present').toBeGreaterThan(-1);
    // Search from AFTER the Expiry label — "<TemplateBar" also appears
    // earlier in this file's own JSDoc prop-documentation comment block.
    const templateBarIdx = CHAIN_TAB.indexOf('<TemplateBar', expiryIdx);
    expect(templateBarIdx, '<TemplateBar mount present after Expiry label').toBeGreaterThan(-1);
    expect(templateBarIdx, '<TemplateBar sits within the Expiry toolbar row')
      .toBeLessThan(expiryIdx + 2500);
    const precedingText = CHAIN_TAB.slice(Math.max(0, templateBarIdx - 600), templateBarIdx);
    expect(precedingText).toMatch(/\{#if showDemoTplNote\}/);
    expect(precedingText).toMatch(/\{:else if showTemplateBar\}/);
    expect(CHAIN_TAB).toMatch(/showTemplateBar\s*=\s*false/);
    expect(CHAIN_TAB).toMatch(/showDemoTplNote\s*=\s*false/);
  });
});
