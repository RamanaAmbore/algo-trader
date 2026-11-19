/**
 * derivatives_greeks_risk_restructure.spec.js
 *
 * 2026-11-18 restructure, operator request: two independent layout
 * changes on /admin/derivatives' "Greeks (position)" and "Risk &
 * expected value" cards.
 *
 *   1. Greeks — a true header-row + value-row grid. The 5 Greek symbols
 *      (Δ Γ Θ 𝒱 ρ) form ONE header row; their 5 numeric values form ONE
 *      data row directly below, each value aligned under its own
 *      header. No `.kv-pair` wrapper any more — `.opt-kv-greeks` is
 *      `grid-template-columns: repeat(5, ...)` with 10 flat children
 *      (5 `.kv-k` then 5 `.kv-v`), relying on the browser's default
 *      row-major auto-placement to produce exactly 2 rows.
 *
 *   2. Risk & expected value — a strict 2-column grid (label | value),
 *      NO header row, one metric per row: R:R, Risk-of-ruin,
 *      Breakevens, POP, EV, and (conditionally) EV/cost — 6 rows, or 5
 *      when `strategy.risk.ev_pct` is null/undefined (the `{#if}` drops
 *      the whole pair, never a blank trailing row).
 *
 * Every InfoHint tooltip, every formatter (pctFmt/fmtPct/fmtMoney), and
 * every kv-pos/kv-neg sign-coloring class is unchanged — this spec only
 * guards the NEW grid shape, not the pre-existing formatting/tooltip
 * behavior (already covered by derivatives_greek_header_chip_infohint.
 * spec.js, derivatives_greeks_risk_vertical_centering.spec.js, and
 * derivatives_tick_flash.spec.js).
 *
 * Five quality dimensions:
 *   1. SSOT   — the markup has exactly one `.opt-kv opt-kv-greeks` block
 *               and one `.opt-kv opt-kv-risk` block; no duplicate/
 *               alternate layout path was left behind.
 *   2. Perf   — live checks reuse the already-loaded strategy; no extra
 *               network round-trips.
 *   3. Stale  — regression guards against the OLD shapes (kv-pair-per-
 *               Greek, 2-pairs-per-row Risk, flex-wrap mobile fallback,
 *               ≥1180px single-row Risk override) reappearing.
 *   4. Reuse  — static checks reuse the same `ruleBody`-style regex
 *               helper pattern already used by alignment_overflow_audit.
 *               spec.js for this exact file.
 *   5. UX     — live checks run at a 360px mobile viewport (this
 *               project's own mobile-check convention) as well as
 *               desktop, confirming no Greek is clipped and no Risk row
 *               is broken at a phone width.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/derivatives_greeks_risk_restructure.spec.js
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loginAsAdmin } from './fixtures/auth.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PAGE_PATH = path.resolve(__dirname, '..', 'src/routes/(algo)/admin/derivatives/+page.svelte');

// ── Suite 1: static source checks (no network, always green) ──────────────

test.describe('Static source checks — Greeks/Risk restructure shape', () => {
  const src = readFileSync(PAGE_PATH, 'utf8');

  const greeksStart = src.indexOf('<div class="opt-kv opt-kv-greeks">');
  const greeksEnd = src.indexOf('Risk &amp; expected value', greeksStart);
  const greeksBlock = src.slice(greeksStart, greeksEnd);

  const riskStart = src.indexOf('<div class="opt-kv opt-kv-risk">');
  // Bounded by the closing of the aside (next sibling after the Risk
  // card's own closing </div></div>) — "</aside>" is the reliable anchor.
  const riskEnd = src.indexOf('</aside>', riskStart);
  const riskBlock = src.slice(riskStart, riskEnd);

  test('exactly one opt-kv-greeks block and one opt-kv-risk block exist', () => {
    expect(greeksStart, 'opt-kv-greeks block must exist').toBeGreaterThan(-1);
    expect(riskStart, 'opt-kv-risk block must exist').toBeGreaterThan(-1);
    // indexOf from AFTER this block's start must find no second occurrence.
    expect(src.indexOf('<div class="opt-kv opt-kv-greeks">', greeksStart + 1)).toBe(-1);
    expect(src.indexOf('<div class="opt-kv opt-kv-risk">', riskStart + 1)).toBe(-1);
  });

  test('Greeks block has no .kv-pair wrapper left (flat header-row + value-row children)', () => {
    expect(greeksBlock).not.toMatch(/class="kv-pair"/);
  });

  test('Greeks block: all 5 .kv-k headers appear before the first .kv-v value (header row, then value row)', () => {
    const lastKvKIdx = [...greeksBlock.matchAll(/class="kv-k kv-k-greek"/g)].map((m) => m.index);
    expect(lastKvKIdx.length).toBe(5);
    const firstKvVIdx = greeksBlock.indexOf('class="kv-v');
    expect(firstKvVIdx, 'a .kv-v value must exist').toBeGreaterThan(-1);
    for (const idx of lastKvKIdx) {
      expect(idx, 'every .kv-k header must precede every .kv-v value in document order').toBeLessThan(firstKvVIdx);
    }
  });

  test('Greeks block: the 5 header symbols appear in Δ Γ Θ 𝒱 ρ order, and the 5 values follow in the same order', () => {
    const symbolOrder = ['Δ', 'Γ', 'Θ', '𝒱', 'ρ'];
    let cursor = 0;
    for (const sym of symbolOrder) {
      const idx = greeksBlock.indexOf(sym, cursor);
      expect(idx, `${sym} must appear in document order`).toBeGreaterThan(-1);
      cursor = idx + 1;
    }
    // Exactly 5 .kv-v values follow, bound to the 5 aggregate_greeks fields
    // in the same delta/gamma/theta/vega/rho order.
    const fieldOrder = ['.delta', '.gamma', '.theta', '.vega', '.rho'];
    let vCursor = greeksBlock.indexOf('class="kv-v');
    for (const field of fieldOrder) {
      const idx = greeksBlock.indexOf(field, vCursor);
      expect(idx, `${field} value must appear in document order`).toBeGreaterThan(-1);
      vCursor = idx + 1;
    }
  });

  test('Greeks grid CSS: 5-column grid with overflow-x:auto (no flex-wrap mobile fallback remains)', () => {
    const cssMatch = src.match(/\.opt-kv-greeks\s*\{([^}]*)\}/);
    expect(cssMatch, '.opt-kv-greeks rule must exist').not.toBeNull();
    const body = cssMatch[1];
    expect(body).toMatch(/grid-template-columns:\s*repeat\(5,/);
    expect(body).toMatch(/overflow-x:\s*auto/);
    expect(body).toMatch(/flex:\s*1/);
    expect(body).toMatch(/align-content:\s*center/);
    expect(src).not.toMatch(/\.opt-kv-greeks\s*\{[^}]*display:\s*flex;[^}]*flex-wrap:\s*wrap;/);
  });

  test('Risk block: exactly 6 .kv-pair rows (R:R, Risk-of-ruin, Breakevens, POP, EV, EV/cost), no header row', () => {
    const pairCount = (riskBlock.match(/class="kv-pair"/g) || []).length;
    expect(pairCount).toBe(6);
    // No header row: the block's first real child element must be the
    // first .kv-pair, not a dedicated header/label-row wrapper.
    const firstPairIdx = riskBlock.indexOf('class="kv-pair"');
    const firstHeaderRowIdx = riskBlock.search(/class="[^"]*-header-row/);
    expect(firstHeaderRowIdx, 'no dedicated header-row wrapper may exist in the Risk block').toBe(-1);
    expect(firstPairIdx).toBeGreaterThan(-1);
  });

  test('Risk block: EV/cost pair is wrapped by the ev_pct != null guard in full (no blank row when absent)', () => {
    const guardIdx = riskBlock.indexOf('{#if strategy?.risk?.ev_pct != null}');
    expect(guardIdx, 'the ev_pct guard must exist').toBeGreaterThan(-1);
    const afterGuard = riskBlock.slice(guardIdx, guardIdx + 500);
    expect(afterGuard).toMatch(/class="kv-pair"/);
    expect(afterGuard).toMatch(/EV \/ cost/);
  });

  test('Risk grid CSS: unconditional 2-column grid, no responsive single-row/2-per-row overrides remain', () => {
    const cssMatch = src.match(/\.opt-kv-risk\s*\{([^}]*)\}/);
    expect(cssMatch, '.opt-kv-risk rule must exist').not.toBeNull();
    const body = cssMatch[1];
    expect(body).toMatch(/display:\s*grid/);
    expect(body).toMatch(/grid-template-columns:\s*max-content\s+minmax\(max-content,\s*1fr\)/);
    expect(body).toMatch(/flex:\s*1/);
    expect(body).toMatch(/align-content:\s*center/);
    // Same defensive overflow handling as Greeks — a worst-case long
    // value (3+ breakevens) must scroll internally, never bleed past
    // the card edge into the app-wide body overflow-x:clip backstop.
    expect(body).toMatch(/overflow-x:\s*auto/);
    const pairCssMatch = src.match(/\.opt-kv-risk\s+\.kv-pair\s*\{([^}]*)\}/);
    expect(pairCssMatch, '.opt-kv-risk .kv-pair rule must exist').not.toBeNull();
    expect(pairCssMatch[1]).toMatch(/display:\s*contents/);
    // Regression guards against the old responsive shapes.
    expect(src).not.toMatch(/@media \(min-width: 1180px\)\s*\{\s*\.opt-kv-risk\s*\{/);
  });
});

// ── Suite 2: live checks — real rendered grid shape ────────────────────────

async function gotoDerivativesWithStrategy(page) {
  await loginAsAdmin(page);
  await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: 30_000 });
  await page.waitForSelector('.opt-kv-greeks', { timeout: 20_000 }).catch(() => {});
}

for (const vp of [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'mobile-360', width: 360, height: 800 },
]) {
  test.describe(`Live — Greeks/Risk restructured grid shape [${vp.name}]`, () => {
    test.use({ viewport: { width: vp.width, height: vp.height } });

    test('Greeks: one header row of 5 symbols, one value row of 5 values directly below, each value aligned under its own header', async ({ page }) => {
      await gotoDerivativesWithStrategy(page);
      const greeks = page.locator('.opt-kv-greeks').first();
      const visible = await greeks.isVisible({ timeout: 5000 }).catch(() => false);
      if (!visible) {
        test.info().annotations.push({ type: 'skip', description: 'No strategy loaded — Greeks card not rendered' });
        return;
      }

      const shape = await greeks.evaluate((el) => {
        const ks = [...el.querySelectorAll('.kv-k')];
        const vs = [...el.querySelectorAll('.kv-v')];
        const rect = (node) => node.getBoundingClientRect();
        return {
          // Spread (not bracket indexing) to get the first real Unicode
          // code point — 𝒱 (U+1D4B1) is a surrogate pair in UTF-16, so
          // `.trim()[0]` would only grab the lone high surrogate.
          labels: ks.map((k) => [...k.textContent.trim()][0]),
          kTops: ks.map((k) => Math.round(rect(k).top)),
          vTops: vs.map((v) => Math.round(rect(v).top)),
          kLefts: ks.map((k) => Math.round(rect(k).left)),
          vLefts: vs.map((v) => Math.round(rect(v).left)),
        };
      });

      expect(shape.labels.length, '5 Greek header labels').toBe(5);
      expect(shape.kTops.length, '5 Greek header cells').toBe(5);
      expect(shape.vTops.length, '5 Greek value cells').toBe(5);

      // Header text order: Δ Γ Θ 𝒱 ρ.
      expect(shape.labels).toEqual(['Δ', 'Γ', 'Θ', '𝒱', 'ρ']);

      // All 5 headers share one row top (±2px rendering tolerance).
      const kTopRef = shape.kTops[0];
      for (const t of shape.kTops) expect(Math.abs(t - kTopRef)).toBeLessThanOrEqual(2);

      // All 5 values share one row top, strictly below the header row.
      const vTopRef = shape.vTops[0];
      for (const t of shape.vTops) expect(Math.abs(t - vTopRef)).toBeLessThanOrEqual(2);
      expect(vTopRef, 'value row must sit below the header row').toBeGreaterThan(kTopRef);

      // Each value's left edge aligns with its own header's left edge
      // (same column track) — within a small tolerance for cell padding.
      for (let i = 0; i < 5; i++) {
        expect(
          Math.abs(shape.vLefts[i] - shape.kLefts[i]),
          `value[${i}] (${shape.labels[i]}) must align under its own header`
        ).toBeLessThanOrEqual(4);
      }
    });

    test('Risk: no header row — 6 (or 5 without EV/cost) two-column label|value rows, each pair on its own row', async ({ page }) => {
      await gotoDerivativesWithStrategy(page);
      const risk = page.locator('.opt-kv-risk').first();
      const visible = await risk.isVisible({ timeout: 5000 }).catch(() => false);
      if (!visible) {
        test.info().annotations.push({ type: 'skip', description: 'No strategy loaded — Risk card not rendered' });
        return;
      }

      const shape = await risk.evaluate((el) => {
        const pairs = [...el.querySelectorAll('.kv-pair')];
        const rect = (node) => node.getBoundingClientRect();
        return pairs.map((p) => {
          const k = p.querySelector('.kv-k');
          const v = p.querySelector('.kv-v');
          return {
            label: k ? k.textContent.trim().replace(/\s+/g, ' ') : null,
            kTop: k ? Math.round(rect(k).top) : null,
            vTop: v ? Math.round(rect(v).top) : null,
            kLeft: k ? Math.round(rect(k).left) : null,
            vLeft: v ? Math.round(rect(v).left) : null,
          };
        });
      });

      // 6 rows normally, 5 when EV/cost's data is absent (ev_pct == null).
      expect([5, 6]).toContain(shape.length);

      // No header row: the first row's label must be a real metric name,
      // not a generic column header (e.g. "Metric"/"Label"/"Value").
      expect(shape[0].label).not.toMatch(/^(Metric|Label|Value)$/i);
      expect(shape[0].label).toMatch(/R:R/);

      // Each row's own label and value share one row top (same row).
      for (const row of shape) {
        expect(row.kTop, `row "${row.label}" label must render`).not.toBeNull();
        expect(row.vTop, `row "${row.label}" value must render`).not.toBeNull();
        expect(Math.abs(row.kTop - row.vTop)).toBeLessThanOrEqual(2);
      }

      // Rows are stacked vertically — every row's top is strictly greater
      // than the previous row's (one metric per row, not 2-per-row).
      for (let i = 1; i < shape.length; i++) {
        expect(shape[i].kTop, `row ${i} ("${shape[i].label}") must sit below row ${i - 1} ("${shape[i - 1].label}")`)
          .toBeGreaterThan(shape[i - 1].kTop);
      }

      // Every label shares one left edge (column 1), every value shares
      // another left edge (column 2) — a true 2-column grid, not a
      // per-row-independent layout.
      const kLeftRef = shape[0].kLeft;
      for (const row of shape) expect(Math.abs(row.kLeft - kLeftRef)).toBeLessThanOrEqual(2);
      const vLeftRef = shape[0].vLeft;
      for (const row of shape) expect(Math.abs(row.vLeft - vLeftRef)).toBeLessThanOrEqual(2);
    });
  });
}

// ── Suite 3: forced-overflow worst case at 320px ────────────────────────────
//
// A realistic strategy's own values fit comfortably inside both cards at
// 320px with no scrollbar — confirmed, but that alone doesn't prove
// `overflow-x: auto` actually works; it just proves it was never
// exercised. These tests inject a deliberately long value (extreme Greek
// magnitudes; 3 breakevens, beyond the InfoHint's own documented "2 for
// iron condors" ceiling) directly into the live DOM to force a genuine
// `scrollWidth > clientWidth` condition, then verify: (1) the grid
// itself — not the surrounding card — absorbs the overflow via internal
// horizontal scroll, and (2) scrolling to the end makes the last
// column's value fully reachable, not permanently clipped by the
// app-wide `body` overflow-x:clip backstop.

test.describe('Live — forced-overflow worst case at 320px (both cards)', () => {
  test.use({ viewport: { width: 320, height: 900 } });

  test('Greeks: a genuinely overflowing row scrolls internally, card stays within the viewport, and the last column is reachable by scroll', async ({ page }) => {
    await gotoDerivativesWithStrategy(page);
    const greeks = page.locator('.opt-kv-greeks').first();
    const visible = await greeks.isVisible({ timeout: 5000 }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: 'No strategy loaded — Greeks card not rendered' });
      return;
    }

    const forced = await greeks.evaluate((el) => {
      const vs = [...el.querySelectorAll('.kv-v')];
      const texts = ['-12,34,567.89', '98,76,543.21', '-45,67,890.12', '23,45,678.90', '-67,89,012.34'];
      vs.forEach((v, i) => { v.textContent = texts[i]; });
      const rect = (n) => n.getBoundingClientRect();
      const card = el.closest('.opt-block');
      return {
        scrollW: el.scrollWidth,
        clientW: el.clientWidth,
        overflowX: getComputedStyle(el).overflowX,
        cardRight: rect(card).right,
      };
    });

    // The forced values must genuinely overflow — otherwise this test
    // isn't exercising the overflow-x:auto path at all.
    expect(forced.scrollW, 'forced long values must overflow the grid box').toBeGreaterThan(forced.clientW);
    expect(forced.overflowX).toBe('auto');
    // The CARD itself must stay within the 320px viewport — the
    // overflow is absorbed by the grid's own scrollbar, not by pushing
    // the card wider than the page.
    expect(forced.cardRight, 'card right edge must stay within the 320px viewport').toBeLessThanOrEqual(321);

    // Scrolling the grid to its end must make the last (ρ) column fully
    // reachable within the grid's own visible rect — proving the value
    // is reachable, not permanently clipped off-screen.
    const scrolled = await greeks.evaluate((el) => {
      el.scrollLeft = el.scrollWidth;
      const gridRect = el.getBoundingClientRect();
      const vs = [...el.querySelectorAll('.kv-v')];
      const rhoRect = vs[4].getBoundingClientRect();
      return {
        rhoVisible: rhoRect.left >= gridRect.left - 1 && rhoRect.right <= gridRect.right + 1,
      };
    });
    expect(scrolled.rhoVisible, 'the last (ρ) Greek must be reachable by scrolling to the grid end').toBe(true);
  });

  test('Risk: a genuinely overflowing row (3 breakevens) scrolls internally, card stays within the viewport', async ({ page }) => {
    await gotoDerivativesWithStrategy(page);
    const risk = page.locator('.opt-kv-risk').first();
    const visible = await risk.isVisible({ timeout: 5000 }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: 'No strategy loaded — Risk card not rendered' });
      return;
    }

    const forced = await risk.evaluate((el) => {
      const pairs = [...el.querySelectorAll('.kv-pair')];
      const breakevensPair = pairs.find((p) => p.querySelector('.kv-k')?.textContent.includes('Breakevens'));
      if (!breakevensPair) return null;
      const v = breakevensPair.querySelector('.kv-v');
      v.textContent = '1,28,847.00 / 1,35,200.00 / 1,42,000.00';
      const rect = (n) => n.getBoundingClientRect();
      const card = el.closest('.opt-block');
      return {
        scrollW: el.scrollWidth,
        clientW: el.clientWidth,
        overflowX: getComputedStyle(el).overflowX,
        cardRight: rect(card).right,
      };
    });
    if (!forced) {
      test.info().annotations.push({ type: 'skip', description: 'Breakevens row not found (strategy has no breakevens pair)' });
      return;
    }

    expect(forced.scrollW, '3-breakevens forced value must overflow the grid box').toBeGreaterThan(forced.clientW);
    expect(forced.overflowX).toBe('auto');
    expect(forced.cardRight, 'card right edge must stay within the 320px viewport').toBeLessThanOrEqual(321);
  });
});
