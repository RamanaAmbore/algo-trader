/**
 * chain_toolbar_trailing_own_row.spec.js
 *
 * Operator: "in chain keep element after symbol expiry in the next row."
 *
 * `OptionChainTab.svelte`'s `.oct-toolbar-trailing` (DTE chip through
 * DRAFT) forces itself onto a fresh second row via `.oct-toolbar`'s
 * `flex-wrap: wrap` plus `.oct-toolbar-trailing`'s own `flex: 0 1 100%`
 * (a 100%-basis flex item always starts a new line in a wrapping flex
 * container). Row 1 is the Expiry picker: a plain `.oct-toolbar-label`
 * ("Expiry") span plus the `.oct-expiry-pick` Select, both direct
 * children of `.oct-toolbar`.
 *
 * AMENDED 2026-11-26 (separate revert, same day) — row 1 used to be
 * `.oct-sym-expiry` ("SYMBOL (EXPIRY)"), the 2026-11-25/26 merge of this
 * tab's Expiry picker with SymbolPanel's Symbol search box. That merge
 * was reverted per a later operator instruction ("revert chain symbol
 * and expiry elements changes and make them two elements in different
 * rows like before") — `.oct-sym-expiry` no longer exists. Symbol is
 * back in its own row in SymbolPanel.svelte's `.oes-picker` (shared by
 * every tab, Chain included); this file's own row-1/row-2 split, below,
 * is UNCHANGED by that revert — it still applies correctly to Expiry
 * alone instead of to the merged "SYMBOL (EXPIRY)" unit. The assertions
 * below were updated in place to reference `.oct-toolbar-label` +
 * `.oct-expiry-pick` (row 1) instead of the retired `.oct-sym-expiry`.
 *
 * This is NOT a regression of the 2026-11-25 "dead empty row" bug —
 * that bug was an EMPTY `<div>` with nothing in it, sitting between two
 * OTHER rows. `.oct-toolbar-trailing` always has real content (the
 * DRAFT `<label>` renders unconditionally, at minimum), so a non-empty
 * second row here is the current, explicit intent.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const CHAIN_TAB_PATH = path.resolve(DIR, '../src/lib/order/OptionChainTab.svelte');

/**
 * Strips `/* ... *\/` block comments before matching a CSS rule's actual
 * declarations — several rules in this file carry long historical
 * comments that legitimately quote OLD property values (e.g. "was
 * `flex-wrap: nowrap`") for context, which would otherwise false-fail a
 * naive "this value must not appear anywhere in the rule" assertion.
 * @param {string} rule
 */
function _declsOnly(rule) {
  return rule.replace(/\/\*[\s\S]*?\*\//g, '');
}

// ──────────────────────────────────────────────────────────────────
// Source-scan — structural/CSS guarantees (no browser needed)
// ──────────────────────────────────────────────────────────────────

test.describe('Toolbar trailing moves to its own row, source-scan', () => {
  let src = '';

  test.beforeAll(() => {
    src = readFileSync(CHAIN_TAB_PATH, 'utf8');
  });

  test('.oct-toolbar is flex-wrap: wrap (keeps row 1 / row 2 split through the Symbol+Expiry revert)', () => {
    const rule = src.match(/\.oct-toolbar\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-toolbar rule').not.toBe('');
    const decls = _declsOnly(rule);
    expect(decls).toMatch(/flex-wrap:\s*wrap;/);
    expect(decls).not.toMatch(/flex-wrap:\s*nowrap/);
  });

  test('.oct-toolbar-trailing has a 100% flex-basis, forcing it onto its own row', () => {
    const rule = src.match(/\.oct-toolbar-trailing\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-toolbar-trailing rule').not.toBe('');
    const decls = _declsOnly(rule);
    expect(decls).toMatch(/flex:\s*0 1 100%/);
    expect(decls).not.toMatch(/flex:\s*0 1 auto/);
    // Internal scroll-not-wrap behaviour for ITS OWN children is unchanged.
    expect(decls).toMatch(/flex-wrap:\s*nowrap/);
    expect(decls).toMatch(/overflow-x:\s*auto/);
  });

  test('row 1 is a plain Expiry label + Select — the retired .oct-sym-expiry/symbolPicker merge markup is gone', () => {
    expect(src).not.toMatch(/\.oct-sym-expiry/);
    // A bare historical mention of the retired prop's name inside a
    // code comment is fine — this checks there is no live declaration
    // or usage left behind.
    expect(src).not.toMatch(/symbolPicker\s*\?:/);
    expect(src).not.toMatch(/symbolPicker\s*=\s*\/\*\*/);
    expect(src).not.toMatch(/\{#if symbolPicker/);
    expect(src).not.toMatch(/\{@render symbolPicker/);
    expect(src).not.toMatch(/oct-expiry-paren/);
    const labelRule = src.match(/\.oct-toolbar-label\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(labelRule, '.oct-toolbar-label rule').not.toBe('');
  });

  test('the comment documenting the wrap decision references the operator instruction and commit 916389fb', () => {
    const idx = src.indexOf('.oct-toolbar {');
    expect(idx, '.oct-toolbar selector not found').toBeGreaterThan(-1);
    const slice = src.slice(idx, idx + 1600);
    // \s+ (not a literal space) between words — the source comment
    // wraps across lines, so "element" and "after" are separated by a
    // newline + indentation in the actual file, not a plain space.
    expect(slice).toMatch(/in chain keep element\s+after/i);
    expect(slice).toMatch(/916389fb/);
  });

  test('DRAFT (and every other trailing control) still renders unconditionally enough that row 2 is never the old empty-row bug', () => {
    // The DRAFT <label> has no {#if} gate around it inside .oct-toolbar —
    // confirming the second row this change creates is never empty.
    const toolbarIdx = src.indexOf('<div class="oct-toolbar">');
    const draftIdx = src.indexOf('class="ot-draft-toggle"', toolbarIdx);
    const toolbarCloseIdx = src.indexOf('\n    </div>', toolbarIdx);
    expect(draftIdx).toBeGreaterThan(toolbarIdx);
    expect(draftIdx).toBeLessThan(toolbarCloseIdx);
    // Walk backward from the draft label to the nearest {#if} — it must
    // belong to the CHASE gate (!draftMode), not a second, closer gate
    // that could make DRAFT itself conditional.
    const nearestIfIdx = src.lastIndexOf('{#if', draftIdx);
    const nearestIfLine = src.slice(nearestIfIdx, src.indexOf('\n', nearestIfIdx));
    expect(nearestIfLine).toMatch(/!draftMode/);
  });
});

// ──────────────────────────────────────────────────────────────────
// Live — real browser, drives the actual Chain tab.
// Reuses this suite's own established navigation precedent (see
// chain_symbol_expiry_merge.spec.js's own `_openNiftyChain`).
// ──────────────────────────────────────────────────────────────────

async function _openNiftyChain(page, sym = 'NIFTY') {
  await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const symInput = page.locator('.ssi-input').first();
  await expect(symInput).toBeVisible({ timeout: 15_000 });
  await symInput.fill(sym);
  const sugg = page.locator('.ssi-drop .ssi-row').first();
  await expect(sugg).toBeVisible({ timeout: 10_000 });
  await sugg.click({ force: true });
  await page.waitForTimeout(500);

  const chainTab = page.getByRole('tab', { name: /Chain/i }).first();
  await expect(chainTab).toBeEnabled({ timeout: 15_000 });
  await chainTab.click();
}

/**
 * Row 1's bounding box — the union of `.oct-toolbar-label` ("Expiry")
 * and `.oct-expiry-pick` (the Select), the two direct children that
 * make up row 1 post-revert. There is no longer a single wrapper div
 * for row 1 (that was `.oct-sym-expiry`, now retired), so this unions
 * the two sibling elements' rects instead of reading one element's box.
 */
async function _row1Box(page) {
  return page.evaluate(() => {
    const label = document.querySelector('.oct-toolbar-label');
    const pick = document.querySelector('.oct-expiry-pick');
    if (!label || !pick) return null;
    const a = label.getBoundingClientRect();
    const b = pick.getBoundingClientRect();
    return {
      x: Math.min(a.left, b.left),
      y: Math.min(a.top, b.top),
      width: Math.max(a.right, b.right) - Math.min(a.left, b.left),
      height: Math.max(a.bottom, b.bottom) - Math.min(a.top, b.top),
    };
  });
}

for (const [label, viewport] of [
  ['desktop (1440px)', { width: 1440, height: 900 }],
  ['mobile (375px)', { width: 375, height: 800 }],
]) {
  test.describe(`Live — ${label}`, () => {
    test.use({ viewport });

    test('.oct-toolbar-trailing renders on its own row, below the Expiry label + Select (row 1)', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      const trailing = page.locator('.oct-toolbar-trailing').first();
      await expect(page.locator('.oct-toolbar-label')).toBeVisible({ timeout: 15_000 });
      await expect(trailing).toBeVisible({ timeout: 15_000 });

      const row1Box = await _row1Box(page);
      const trailingBox = await trailing.boundingBox();
      expect(row1Box, 'row 1 (Expiry label + Select) box').toBeTruthy();
      expect(trailingBox, '.oct-toolbar-trailing box').toBeTruthy();

      // Not overlapping, not the same row — trailing's top is at or
      // below row 1's own bottom.
      expect(trailingBox.y, `trailing top (${trailingBox.y}) overlaps row 1 bottom (${row1Box.y + row1Box.height})`)
        .toBeGreaterThanOrEqual(row1Box.y + row1Box.height - 1);

      // Still left-aligned with row 1 (no rightward drift).
      expect(Math.abs(trailingBox.x - row1Box.x), 'row 2 left edge should match row 1 left edge')
        .toBeLessThan(4);
    });

    // Trailing's own internal scroll still works on its own, now
    // full-width, row (doesn't regress now that it's no longer squeezed
    // against row 1 on the same row).
    test('.oct-toolbar-trailing keeps its internal controls on one line (scrolls, never wraps internally)', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      const trailing = page.locator('.oct-toolbar-trailing').first();
      await expect(trailing).toBeVisible({ timeout: 15_000 });
      await expect(page.locator('.oct-expiry-dte')).toBeVisible({ timeout: 15_000 });

      const info = await trailing.evaluate((el) => {
        const kids = Array.from(el.children).filter((c) => {
          const r = c.getBoundingClientRect();
          return r.width > 0 && r.height > 0;
        });
        const tops = kids.map((c) => c.getBoundingClientRect().top);
        return {
          maxTop: Math.max(...tops),
          minTop: Math.min(...tops),
          scrollWidth: el.scrollWidth,
          clientWidth: el.clientWidth,
        };
      });
      // All visible direct children still share one line (no internal
      // wrap) — a loose tolerance since align-items:center can vary top
      // slightly across differently-sized children.
      expect(info.maxTop - info.minTop, `trailing children tops vary by ${info.maxTop - info.minTop}px — looks like an internal wrap`)
        .toBeLessThan(20);
    });
  });
}
