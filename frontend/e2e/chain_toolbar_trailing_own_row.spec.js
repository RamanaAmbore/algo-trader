/**
 * chain_toolbar_trailing_own_row.spec.js
 *
 * Operator: "in chain keep element after symbol expiry in the next row."
 *
 * `OptionChainTab.svelte`'s `.oct-toolbar-trailing` (DTE chip through
 * DRAFT) used to share ONE row with `.oct-sym-expiry` ("SYMBOL
 * (EXPIRY)"), via `.oct-toolbar`'s `flex-wrap: nowrap`. This reverses
 * that: `.oct-toolbar` is now `flex-wrap: wrap`, and
 * `.oct-toolbar-trailing` forces itself onto a fresh second row via
 * `flex: 0 1 100%` (a 100%-basis flex item always starts a new line in
 * a wrapping flex container). `.oct-sym-expiry` keeps `flex: 0 0 auto`
 * and alone occupies row 1.
 *
 * This is NOT a regression of the 2026-11-25 "dead empty row" bug —
 * that bug was an EMPTY `<div>` with nothing in it, sitting between two
 * OTHER rows. `.oct-toolbar-trailing` always has real content (the
 * DRAFT `<label>` renders unconditionally, at minimum), so a non-empty
 * second row here is the current, explicit intent.
 *
 * `chain_symbol_expiry_merge.spec.js`'s own pre-existing assertions for
 * the OLD nowrap/single-row shape were updated in place (not deleted) —
 * see that file's own "AMENDED 2026-11-26" notes. This file adds
 * dedicated, focused coverage for the row-break itself.
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

  test('.oct-toolbar is flex-wrap: wrap (reversed from the 2026-11-25 nowrap fix)', () => {
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

  test('.oct-sym-expiry keeps flex: 0 0 auto — unaffected by the trailing-row change', () => {
    const rule = src.match(/\.oct-sym-expiry\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-sym-expiry rule').not.toBe('');
    expect(_declsOnly(rule)).toMatch(/flex:\s*0 0 auto/);
  });

  test('the comment documenting the wrap decision reflects the reversal, not the old nowrap rationale', () => {
    const idx = src.indexOf('.oct-toolbar {');
    expect(idx, '.oct-toolbar selector not found').toBeGreaterThan(-1);
    // The documenting comment lives INSIDE the rule body (between
    // `gap: 0.4rem;` and `flex-wrap: wrap;`), not before the selector.
    const slice = src.slice(idx, idx + 2600);
    expect(slice).toMatch(/Reversed 2026-11-26/);
    expect(slice).toMatch(/in chain keep element after/i);
    // The old "never wrap" framing is referenced only historically now
    // (as superseded), not asserted as the live, standalone behaviour.
    expect(slice).toMatch(/could never wrap/i);
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
    // belong to the outer toolbar gate (symbolPicker || ...), not a
    // second, closer gate that could make DRAFT itself conditional.
    const nearestIfIdx = src.lastIndexOf('{#if', draftIdx);
    const nearestIfLine = src.slice(nearestIfIdx, src.indexOf('\n', nearestIfIdx));
    expect(nearestIfLine).toMatch(/!draftMode|symbolPicker \|\|/);
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

for (const [label, viewport] of [
  ['desktop (1440px)', { width: 1440, height: 900 }],
  ['mobile (375px)', { width: 375, height: 800 }],
]) {
  test.describe(`Live — ${label}`, () => {
    test.use({ viewport });

    test('.oct-toolbar-trailing renders on its own row, below .oct-sym-expiry', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      const symExpiry = page.locator('.oct-sym-expiry').first();
      const trailing = page.locator('.oct-toolbar-trailing').first();
      await expect(symExpiry).toBeVisible({ timeout: 15_000 });
      await expect(trailing).toBeVisible({ timeout: 15_000 });

      const symExpiryBox = await symExpiry.boundingBox();
      const trailingBox = await trailing.boundingBox();
      expect(symExpiryBox, '.oct-sym-expiry box').toBeTruthy();
      expect(trailingBox, '.oct-toolbar-trailing box').toBeTruthy();

      // Not overlapping, not the same row — trailing's top is at or
      // below sym-expiry's own bottom.
      expect(trailingBox.y, `trailing top (${trailingBox.y}) overlaps sym-expiry bottom (${symExpiryBox.y + symExpiryBox.height})`)
        .toBeGreaterThanOrEqual(symExpiryBox.y + symExpiryBox.height - 1);

      // Still left-aligned with row 1 (no rightward drift).
      expect(Math.abs(trailingBox.x - symExpiryBox.x), 'row 2 left edge should match row 1 left edge')
        .toBeLessThan(4);
    });

    // Trailing's own internal scroll still works on its own, now
    // full-width, row (doesn't regress now that it's no longer squeezed
    // against .oct-sym-expiry on the same row).
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
