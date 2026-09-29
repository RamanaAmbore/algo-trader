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
    expect(src).toMatch(/class:active=\{_toggleOn\}/);
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

  test('toggle click handler routes to onSelectNone when active, onSelectDefault when inactive', () => {
    const src = readFileSync(TEMPLATE_BAR_PATH, 'utf8');
    const btn = src.match(/class="oes-tpl-button"[\s\S]{0,600}?<\/button>/)?.[0] ?? '';
    expect(btn).toContain('onSelectNone?.()');
    expect(btn).toContain('onSelectDefault?.()');
    expect(btn).toMatch(/if\s*\(_toggleOn\)/);
  });

  test('toggle button is disabled only while inactive with no side-aware default (never while active)', () => {
    const src = readFileSync(TEMPLATE_BAR_PATH, 'utf8');
    expect(src).toMatch(/_templBtnDisabled\s*=\s*\$derived\(!_toggleOn\s*&&\s*_toggleOnDisabled\)/);
    expect(src).toMatch(/disabled=\{_templBtnDisabled\}/);
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
