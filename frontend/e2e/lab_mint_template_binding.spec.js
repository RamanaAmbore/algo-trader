/**
 * lab_mint_template_binding.spec.js
 *
 * 2026-11 (Track 3, T3.4, Bracket/Template/GTT removal) — this spec used
 * to verify the "Exit bracket" / `template_slug` binding on the MCP
 * page's mint form (commit ac4d08c9). That binding is removed: the
 * backend's `load_template_for_slug_or_id` resolver
 * (`backend/api/algo/template_attach.py`) is deleted along with the rest
 * of the Bracket engine, so the field is now a dead param with no
 * server-side effect. Rewritten as a source-level regression guard
 * confirming the field, its payload binding, and its explanatory text
 * are all gone — not just coincidentally never exercised.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const PAGE_SRC = readFileSync(
  new URL('../src/routes/(algo)/admin/mcp/+page.svelte', import.meta.url).pathname, 'utf8'
);

test.describe('MCP mint form — template_slug / Exit bracket binding removed', () => {
  test('mintForm no longer carries a template_slug field', () => {
    expect(PAGE_SRC).not.toMatch(/template_slug:\s*''/);
    expect(PAGE_SRC).not.toMatch(/mintForm\.template_slug/);
  });

  test('the mint payload builder no longer sends template_slug', () => {
    const mintStart = PAGE_SRC.indexOf('async function mint()');
    expect(mintStart).toBeGreaterThan(-1);
    const mintEnd = PAGE_SRC.indexOf('\n  }', mintStart);
    const mintBody = PAGE_SRC.slice(mintStart, mintEnd);
    expect(mintBody).not.toMatch(/template_slug/);
  });

  test('no "Exit bracket" input exists in the place-kind form fields', () => {
    expect(PAGE_SRC).not.toMatch(/Exit bracket/);
    expect(PAGE_SRC).not.toMatch(/\(none\) e\.g\. default-bull/);
  });

  test('the mint-widget help text and Safety card no longer describe exit-bracket token binding', () => {
    expect(PAGE_SRC).not.toMatch(/exit bracket is part of the token too/);
    expect(PAGE_SRC).not.toMatch(/exit bracket — new qty\/price too for modify/);
  });
});
