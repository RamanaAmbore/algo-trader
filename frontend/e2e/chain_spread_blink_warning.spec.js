/**
 * chain_spread_blink_warning.spec.js
 *
 * Operator: "when spread is more you can blink the text now for spread
 * refresh. but keep the place it anyway static."
 *
 * The `⚠` warning icon on a strike's CE/PE bid-ask cell (OptionChainTab's
 * `.chain-cell-spread-warn`) used to be conditionally removed from the
 * DOM entirely, so its appearance/disappearance on each 30s quote poll
 * (`_refreshChainQuotes`) shifted the bid/ask text beside it. Fixed:
 *   - The `.chain-cell-spread-warn` span is now ALWAYS in the DOM (never
 *     conditionally removed), with a fixed `width` reserving its own
 *     slot.
 *   - `visibility: hidden` (not `display: none`) hides it when the
 *     spread isn't wide — same reserved box either way.
 *   - A `class:chain-cell-spread-warn-on={ceSpreadWide}` toggle drives
 *     an infinite opacity-blink keyframe animation while the spread IS
 *     wide. The underlying `ceSpreadWide`/`peSpreadWide` derived values
 *     are already re-evaluated on every quote poll, so the class (and
 *     therefore the animation) naturally re-confirms "still wide" on
 *     every refresh with no extra JS.
 *   - `title`/`aria-hidden` are gated on the same boolean so an
 *     undefined quote (`ceQ`/`peQ` not yet loaded) never throws
 *     computing the tooltip text.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const CHAIN_TAB_PATH = path.resolve(DIR, '../src/lib/order/OptionChainTab.svelte');

// ──────────────────────────────────────────────────────────────────
// Source-scan — structural/CSS guarantees (no browser needed)
// ──────────────────────────────────────────────────────────────────

test.describe('Spread-warn blink, source-scan', () => {
  let src = '';

  test.beforeAll(() => {
    src = readFileSync(CHAIN_TAB_PATH, 'utf8');
  });

  test('no {#if ceSpreadWide}/{#if peSpreadWide} wrapper remains around the warning span', () => {
    expect(src).not.toMatch(/\{#if ceSpreadWide\}/);
    expect(src).not.toMatch(/\{#if peSpreadWide\}/);
  });

  test('exactly 4 chain-cell-spread-warn spans exist (CE + PE, ATM row + non-ATM row)', () => {
    const matches = src.match(/class="chain-cell-spread-warn"/g) || [];
    expect(matches.length).toBe(4);
  });

  test('each warning span carries class:chain-cell-spread-warn-on bound to its own derived flag', () => {
    const ceMatches = src.match(/class:chain-cell-spread-warn-on=\{ceSpreadWide\}/g) || [];
    const peMatches = src.match(/class:chain-cell-spread-warn-on=\{peSpreadWide\}/g) || [];
    expect(ceMatches.length).toBe(2);
    expect(peMatches.length).toBe(2);
  });

  test('title and aria-hidden are gated on the spread flag — never crash on an undefined quote', () => {
    const titleMatches = src.match(/title=\{ceSpreadWide \? `Wide spread[\s\S]*?: undefined\}/g) || [];
    expect(titleMatches.length).toBeGreaterThanOrEqual(1);
    const ariaMatches = src.match(/aria-hidden=\{!ceSpreadWide\}/g) || [];
    expect(ariaMatches.length).toBe(2);
    const ariaMatchesPe = src.match(/aria-hidden=\{!peSpreadWide\}/g) || [];
    expect(ariaMatchesPe.length).toBe(2);
  });

  test('.chain-cell-spread-warn reserves a fixed width and hides via visibility, not opacity/display', () => {
    const rule = src.match(/\.chain-cell-spread-warn\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.chain-cell-spread-warn rule').not.toBe('');
    expect(rule).toMatch(/display:\s*inline-block/);
    expect(rule).toMatch(/width:\s*\S+/);
    expect(rule).toMatch(/visibility:\s*hidden/);
    expect(rule).not.toMatch(/display:\s*none/);
  });

  test('.chain-cell-spread-warn-on flips visibility and runs the blink animation', () => {
    const rule = src.match(/\.chain-cell-spread-warn-on\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.chain-cell-spread-warn-on rule').not.toBe('');
    expect(rule).toMatch(/visibility:\s*visible/);
    expect(rule).toMatch(/animation:\s*chain-spread-warn-blink/);
    expect(rule).toMatch(/infinite/);
  });

  test('the blink keyframes alternate opacity, and are disabled under prefers-reduced-motion', () => {
    expect(src).toMatch(/@keyframes chain-spread-warn-blink\s*\{[\s\S]*?opacity:\s*1[\s\S]*?opacity:\s*0\.\d+[\s\S]*?\}/);
    const reducedBlock = src.match(/@media \(prefers-reduced-motion:\s*reduce\)\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(reducedBlock).toMatch(/\.chain-cell-spread-warn-on\s*\{\s*animation:\s*none;\s*\}/);
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

    // Deterministic layout-stability check — off-hours or a tight market
    // may have no genuinely wide-spread strike at test time, so this does
    // not rely on live data landing in that state. Instead it picks a
    // real rendered `.chain-cell-spread-warn` span (always present now,
    // win or lose), records its sibling `.chain-cell-ask` box and the
    // row height, force-adds `chain-cell-spread-warn-on` via
    // `page.evaluate` (the element already carries this real class,
    // Svelte's scope hash included, so toggling the class name directly
    // is valid), then re-measures and asserts nothing moved.
    test('spread-warn icon toggling visible does not shift the bid/ask text beside it', async ({ page }) => {
      await loginAsAdmin(page);
      await _openNiftyChain(page);

      const warnSpan = page.locator('.chain-cell-spread-warn').first();
      await expect(warnSpan).toHaveCount(1, { timeout: 15_000 });

      const before = await warnSpan.evaluate((el) => {
        const row = el.closest('tr');
        const ask = row.querySelector('.chain-cell-ask');
        const askRect = ask.getBoundingClientRect();
        const rowRect = row.getBoundingClientRect();
        return {
          askLeft: askRect.left, askTop: askRect.top,
          rowHeight: rowRect.height,
          visibility: getComputedStyle(el).visibility,
        };
      });

      // Force the "wide spread" visual state on, regardless of real
      // live quote data.
      await warnSpan.evaluate((el) => { el.classList.add('chain-cell-spread-warn-on'); });
      await page.waitForTimeout(50);

      const after = await warnSpan.evaluate((el) => {
        const row = el.closest('tr');
        const ask = row.querySelector('.chain-cell-ask');
        const askRect = ask.getBoundingClientRect();
        const rowRect = row.getBoundingClientRect();
        return {
          askLeft: askRect.left, askTop: askRect.top,
          rowHeight: rowRect.height,
          visibility: getComputedStyle(el).visibility,
          animationName: getComputedStyle(el).animationName,
        };
      });

      expect(before.visibility, 'icon must start hidden (no real wide spread forced yet)').toBe('hidden');
      expect(after.visibility, 'icon must become visible once the -on class is applied').toBe('visible');
      expect(after.animationName, 'blink animation must be running while visible').not.toBe('none');

      // The load-bearing assertion: toggling the icon's visibility must
      // not move its own sibling .chain-cell-ask text, nor change the
      // row's own height.
      expect(after.askLeft, `ask cell left shifted: ${before.askLeft} -> ${after.askLeft}`)
        .toBeCloseTo(before.askLeft, 1);
      expect(after.askTop, `ask cell top shifted: ${before.askTop} -> ${after.askTop}`)
        .toBeCloseTo(before.askTop, 1);
      expect(after.rowHeight, `row height changed: ${before.rowHeight} -> ${after.rowHeight}`)
        .toBeCloseTo(before.rowHeight, 1);
    });
  });
}
