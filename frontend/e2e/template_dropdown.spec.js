/**
 * template_dropdown.spec.js
 *
 * Guards the template-attach toggle's evolution in TemplateBar.svelte.
 * History (each stage fully replaced the last): two-pill toggle →
 * Select dropdown (2026-09-29) → Default/None two-button ON/OFF pill
 * (2026-09-30) → single toggle button (2026-09-30, operator: "make
 * Templ look like a button which can be active or inactive based on
 * button press, default active"). This file previously guarded the
 * Select-dropdown stage, which no longer exists — rewritten to guard
 * the CURRENT single-button design instead.
 *
 * A specific named template (as opposed to the side-aware default) is
 * still picked via a compact `<Select>` inside the expand panel — that
 * part of the "dropdown" name remains accurate.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const TEMPLATE_BAR_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/TemplateBar.svelte',
);
const SYMBOL_PANEL_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/SymbolPanel.svelte',
);

test.describe('Stale-code: Template label abbreviated to "Templ"', () => {
  test('TemplateBar.svelte\'s toggle button says "Templ", not "Template"', () => {
    const tplSrc = readFileSync(TEMPLATE_BAR_PATH, 'utf8');
    // The label lives on the toggle button itself since the 2026-09-30
    // single-button redesign — SymbolPanel.svelte no longer carries any
    // Templ-related markup at all (relocated into OptionChainTab →
    // TemplateBar; see the removal comment near .oes-basket-tpl-row-demo
    // in SymbolPanel.svelte).
    expect(tplSrc).toMatch(/class="oes-tpl-button"[\s\S]{0,700}?>\s*Templ\s*</);
    expect(tplSrc).not.toContain('>Template<');
  });

  test('SymbolPanel.svelte carries no leftover Templ-label markup (relocated to TemplateBar)', () => {
    const panelSrc = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(panelSrc).not.toContain('oes-basket-tpl-label">Templ<');
  });
});

test.describe('Stale-code: single toggle button replaces the old Default/None pill', () => {
  test('TemplateBar renders one Templ toggle button, not the retired two-button pill', () => {
    const src = readFileSync(TEMPLATE_BAR_PATH, 'utf8');
    expect(src).toContain('class="oes-tpl-button"');
    // TEMP DEBUG MODE (2026-09-30, see below) — button currently reads
    // _debugOn, not _toggleOn.
    expect(src).toMatch(/class:active=\{_debugOn\}/);
    // Old two-button pill markup/classes must be gone.
    expect(src).not.toContain('oes-tpl-toggle-btn-on');
    expect(src).not.toContain('oes-tpl-toggle-btn-off');
    expect(src).not.toContain('class="oes-tpl-toggle"');
    // Older still: the Select-dropdown stage must also be gone from the
    // main toggle (the expand panel's "Specific tmpl" Select is a
    // separate, still-current feature — checked below, not asserted
    // absent here).
    expect(src).not.toMatch(/_onDropdownChange/);
  });

  test('TEMP DEBUG MODE — toggle is a plain local on/off, not wired to onSelectDefault/onSelectNone', () => {
    // Operator: "just display templ with on and off behavior without
    // wiring with functionality... Once I confirm, then wire it
    // functionality." Deliberate, temporary — REVERT alongside the
    // source block it guards once the display issue is confirmed fixed
    // and the button is wired back to the real handlers below.
    const src = readFileSync(TEMPLATE_BAR_PATH, 'utf8');
    const btn = src.match(/class="oes-tpl-button"[\s\S]{0,600}?<\/button>/)?.[0] ?? '';
    expect(btn).not.toContain('onSelectNone?.()');
    expect(btn).not.toContain('onSelectDefault?.()');
    expect(btn).toContain('_debugToggleClick');
    // The real handlers are still declared as props (available to
    // re-wire) even though nothing currently calls them.
    expect(src).toMatch(/^\s*onSelectDefault,/m);
    expect(src).toMatch(/^\s*onSelectNone,/m);
  });

  test('TEMP DEBUG MODE — toggle button is never disabled (real disabled-gating logic untouched but disconnected)', () => {
    const src = readFileSync(TEMPLATE_BAR_PATH, 'utf8');
    const btn = src.match(/class="oes-tpl-button"[\s\S]{0,600}?<\/button>/)?.[0] ?? '';
    expect(btn).not.toMatch(/disabled=/);
    // The real gating derivation must still exist in the script, just
    // disconnected from the button's `disabled` attribute.
    expect(src).toMatch(/_templBtnDisabled\s*=\s*\$derived\(!_toggleOn\s*&&\s*_toggleOnDisabled\)/);
  });

  test('default state is active whenever a side-aware default resolves (_toggleOn derivation unchanged)', () => {
    const src = readFileSync(TEMPLATE_BAR_PATH, 'utf8');
    expect(src).toMatch(/_toggleOn\s*=\s*\$derived\(!shellUsingNone\s*&&\s*!!selectedTemplate\)/);
  });

  test('the expand-panel "Specific tmpl" Select (a genuinely separate feature) is still present', () => {
    const src = readFileSync(TEMPLATE_BAR_PATH, 'utf8');
    expect(src).toContain('<Select');
    expect(src).toMatch(/nonNoneTemplates\.map/);
    expect(src).toContain('onSelectTemplate?.(Number(v))');
  });

  test('SymbolPanel passes nonNoneTemplates and onSelectTemplate down through OptionChainTab', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(src).toMatch(/nonNoneTemplates=\{_nonNoneTemplates\}/);
    expect(src).toMatch(/onSelectTemplate=\{\(id\)\s*=>\s*\{\s*_sharedTemplateId\s*=\s*id;\s*\}\}/);
  });
});
