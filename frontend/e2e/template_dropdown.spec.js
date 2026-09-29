/**
 * template_dropdown.spec.js
 *
 * Guards the 2026-09-29 template-picker redesign: operator explicitly
 * asked to reverse the prior "no dropdown — the platform picks the
 * template" decision and add a real dropdown showing every active
 * template by name, plus abbreviate the "Template" label to "Templ".
 *
 * Replaces the old Default/None two-pill toggle + separate name chip
 * with a single Select whose options are Default / None / every named
 * template, reusing the exact options-shape pattern already used by
 * the per-leg template override editor in SymbolPanel.svelte.
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
  test('TemplateBar.svelte and the demo-mode row both say "Templ", not "Template"', () => {
    const tplSrc = readFileSync(TEMPLATE_BAR_PATH, 'utf8');
    const panelSrc = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(tplSrc).toContain('oes-basket-tpl-label">Templ<');
    expect(panelSrc).toContain('oes-basket-tpl-label">Templ<');
    expect(tplSrc).not.toContain('>Template<');
    expect(panelSrc).not.toContain('>Template<');
  });
});

test.describe('Stale-code: Template dropdown replaces Default/None pill toggle', () => {
  test('TemplateBar renders a Select with Default/None/named-template options, not the old pill buttons', () => {
    const src = readFileSync(TEMPLATE_BAR_PATH, 'utf8');
    expect(src).toContain('<Select');
    expect(src).toMatch(/value:\s*'default'/);
    expect(src).toMatch(/value:\s*'none'/);
    expect(src).toMatch(/nonNoneTemplates\.map/);
    // Old pill-toggle markup must be gone.
    expect(src).not.toContain('oes-tpl-btn-default');
    expect(src).not.toContain('oes-tpl-btn-none');
    expect(src).not.toContain('onclick={onSelectDefault}');
  });

  test('dropdown change handler routes default/none/id to the right callback', () => {
    const src = readFileSync(TEMPLATE_BAR_PATH, 'utf8');
    const fn = src.match(/function _onDropdownChange[\s\S]{0,300}?\n  \}/)?.[0] ?? '';
    expect(fn).toContain('onSelectDefault?.()');
    expect(fn).toContain('onSelectNone?.()');
    expect(fn).toContain('onSelectTemplate?.(Number(v))');
  });

  test('SymbolPanel passes nonNoneTemplates and onSelectTemplate down to TemplateBar', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(src).toMatch(/nonNoneTemplates=\{_nonNoneTemplates\}/);
    expect(src).toMatch(/onSelectTemplate=\{\(id\)\s*=>\s*\{\s*_sharedTemplateId\s*=\s*id;\s*\}\}/);
  });
});
