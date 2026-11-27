/**
 * chain_toolbar_trailing_own_row.spec.js
 *
 * Operator: "in chain keep element after symbol expiry in the next row"
 * (commit 916389fb) — originally put the DTE chip through DRAFT on a
 * SECOND row below a plain "Expiry label + Select" row 1.
 *
 * SUPERSEDED same day, 2026-11-26 (this revision) — a later, more
 * specific operator instruction: "move expires today and spread to the
 * same row as expiry". Row 1 now carries the Expiry label + Select
 * PLUS the DTE chip and Spread% field, wrapped in a new
 * `.oct-toolbar-row1` container (its own `flex-wrap: nowrap;
 * overflow-x: auto` scroller, same established pattern
 * `.oct-toolbar-trailing` already used). `.oct-toolbar-trailing`
 * (unchanged name) keeps its own row below, now holding CHASE / DRAFT /
 * HEDGE (+ B%/S% fields) — HEDGE moved to AFTER DRAFT in the same pass
 * (operator: "reorder: hedge after draft").
 *
 * `.oct-toolbar` itself is no longer the single flex-wrap:wrap row that
 * used a 100%-basis trick to force a second line — it's now a plain
 * `flex-direction: column` wrapper holding two explicit, independently
 * scrollable rows.
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
 * comments that legitimately quote OLD property values for context,
 * which would otherwise false-fail a naive substring assertion.
 * @param {string} rule
 */
function _declsOnly(rule) {
  return rule.replace(/\/\*[\s\S]*?\*\//g, '');
}

// ──────────────────────────────────────────────────────────────────
// Source-scan — structural/CSS guarantees (no browser needed)
// ──────────────────────────────────────────────────────────────────

test.describe('Toolbar row 1 gains DTE/Spread%; row 2 keeps CHASE/DRAFT/HEDGE, source-scan', () => {
  let src = '';

  test.beforeAll(() => {
    src = readFileSync(CHAIN_TAB_PATH, 'utf8');
  });

  test('.oct-toolbar is a column wrapper — the old single-row flex-wrap:wrap trick is gone', () => {
    const rule = src.match(/\.oct-toolbar\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-toolbar rule').not.toBe('');
    const decls = _declsOnly(rule);
    expect(decls).toMatch(/flex-direction:\s*column;/);
    expect(decls).not.toMatch(/flex-wrap:\s*wrap/);
  });

  test('.oct-toolbar-row1 exists, holding Expiry label/Select + DTE + Spread%, as its own nowrap/overflow-x:auto scroller', () => {
    const rule = src.match(/\.oct-toolbar-row1\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-toolbar-row1 rule').not.toBe('');
    const decls = _declsOnly(rule);
    expect(decls).toMatch(/flex-wrap:\s*nowrap/);
    expect(decls).toMatch(/overflow-x:\s*auto/);

    const markupIdx = src.indexOf('<div class="oct-toolbar-row1">');
    expect(markupIdx, 'oct-toolbar-row1 markup not found').toBeGreaterThan(-1);
    // The row's OWN closing tag, not the first nested `</div>` (the
    // Select wrapper `.oct-expiry-pick` closes well before DTE/Spread%).
    const row1End = src.indexOf('\n      </div>', markupIdx);
    const row1Markup = src.slice(markupIdx, row1End);
    expect(row1Markup).toMatch(/oct-toolbar-label/);
    expect(row1Markup).toMatch(/oct-expiry-pick/);
    expect(row1Markup).toMatch(/oct-expiry-dte/);
    expect(row1Markup).toMatch(/oct-spread-field/);
  });

  test('.oct-toolbar-trailing no longer forces a 100%-basis new line (the column wrapper handles row separation now), and keeps its own internal scroll', () => {
    const rule = src.match(/\.oct-toolbar-trailing\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oct-toolbar-trailing rule').not.toBe('');
    const decls = _declsOnly(rule);
    expect(decls).not.toMatch(/flex:\s*0 1 100%/);
    expect(decls).toMatch(/flex-wrap:\s*nowrap/);
    expect(decls).toMatch(/overflow-x:\s*auto/);
  });

  test('row 2 order is CHASE, then DRAFT, then HEDGE (+ B%/S% fields) — HEDGE moved after DRAFT', () => {
    const trailingStart = src.indexOf('<div class="oct-toolbar-trailing">');
    expect(trailingStart, 'oct-toolbar-trailing markup not found').toBeGreaterThan(-1);
    const trailingEnd = src.indexOf('\n      </div>', trailingStart);
    const trailingMarkup = src.slice(trailingStart, trailingEnd);

    const chaseIdx = trailingMarkup.indexOf('oes-chase-cluster');
    const draftIdx = trailingMarkup.indexOf('ot-draft-toggle');
    const hedgeIdx = trailingMarkup.indexOf('oct-hedge-toggle');
    expect(chaseIdx).toBeGreaterThan(-1);
    expect(draftIdx).toBeGreaterThan(-1);
    expect(hedgeIdx).toBeGreaterThan(-1);
    expect(draftIdx).toBeGreaterThan(chaseIdx);
    expect(hedgeIdx).toBeGreaterThan(draftIdx);
  });

  test('DRAFT still renders unconditionally (never the old empty-row bug) and is never disabled by Hedge', () => {
    const toolbarIdx = src.indexOf('<div class="oct-toolbar">');
    const draftIdx = src.indexOf('class="ot-draft-toggle"', toolbarIdx);
    const toolbarCloseIdx = src.indexOf('\n    </div>', toolbarIdx);
    expect(draftIdx).toBeGreaterThan(toolbarIdx);
    expect(draftIdx).toBeLessThan(toolbarCloseIdx);

    // The DRAFT <label>'s own markup block must never carry a `disabled`
    // binding — only HEDGE does, per the asymmetric mutual-exclusivity
    // rule (Draft always wins; Draft is never disabled by Hedge).
    const draftLabelBlock = src.slice(draftIdx, src.indexOf('</label>', draftIdx));
    expect(draftLabelBlock).not.toMatch(/disabled=/);
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

    test('DTE chip and Spread% field render on the SAME row as the Expiry label + Select', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      const row1 = page.locator('.oct-toolbar-row1').first();
      await expect(row1).toBeVisible({ timeout: 15_000 });
      await expect(row1.locator('.oct-toolbar-label')).toBeVisible();
      await expect(row1.locator('.oct-expiry-pick')).toBeVisible();
      await expect(row1.locator('.oct-expiry-dte')).toBeVisible();
      await expect(row1.locator('.oct-spread-field')).toBeVisible();

      // All four sit on one visual row — tops within a loose tolerance.
      const tops = await row1.evaluate((el) => {
        const kids = Array.from(el.children).filter((c) => {
          const r = c.getBoundingClientRect();
          return r.width > 0 && r.height > 0;
        });
        return kids.map((c) => c.getBoundingClientRect().top);
      });
      expect(Math.max(...tops) - Math.min(...tops), 'row 1 children should share one visual line').toBeLessThan(20);
    });

    test('.oct-toolbar-trailing (CHASE/DRAFT/HEDGE) renders on its own row, below row 1', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      const row1 = page.locator('.oct-toolbar-row1').first();
      const trailing = page.locator('.oct-toolbar-trailing').first();
      await expect(row1).toBeVisible({ timeout: 15_000 });
      await expect(trailing).toBeVisible({ timeout: 15_000 });

      const row1Box = await row1.boundingBox();
      const trailingBox = await trailing.boundingBox();
      expect(row1Box, 'row 1 box').toBeTruthy();
      expect(trailingBox, '.oct-toolbar-trailing box').toBeTruthy();

      expect(trailingBox.y, `trailing top (${trailingBox.y}) overlaps row 1 bottom (${row1Box.y + row1Box.height})`)
        .toBeGreaterThanOrEqual(row1Box.y + row1Box.height - 1);
      expect(Math.abs(trailingBox.x - row1Box.x), 'row 2 left edge should match row 1 left edge')
        .toBeLessThan(4);
    });

    test('row 2 order is CHASE, DRAFT, then HEDGE, left to right', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      const trailing = page.locator('.oct-toolbar-trailing').first();
      await expect(trailing).toBeVisible({ timeout: 15_000 });

      const order = await trailing.evaluate((el) => {
        const children = Array.from(el.children);
        const findIdx = (sel) => children.findIndex((c) => c.matches(sel) || c.querySelector(sel));
        return {
          chase: findIdx('.oes-chase-cluster'),
          draft: findIdx('.ot-draft-toggle'),
          hedge: findIdx('.oct-hedge-toggle'),
        };
      });
      expect(order.chase).toBeGreaterThanOrEqual(0);
      expect(order.draft).toBeGreaterThan(order.chase);
      expect(order.hedge).toBeGreaterThan(order.draft);
    });
  });
}
