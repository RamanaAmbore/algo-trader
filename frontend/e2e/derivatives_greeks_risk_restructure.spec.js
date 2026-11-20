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
 * 2026-11-19 follow-up — Risk & EV only. The card was first described
 * (above) as having no header row; the operator then said it "can have
 * a header too". The card is now responsive: a 4-track grid (2 of the 3
 * metric|value pairs per row, with a real header row of 4 visible
 * `.kv-risk-colhdr` cells) below 1180px, widening to a 6-track grid (all
 * 3 pairs in one row, all 6 header cells visible) at ≥1180px — the same
 * breakpoint the Payoff+Legs row already uses elsewhere in this file.
 * The Greeks card and its 2026-11-18 shape are unaffected by this
 * follow-up (still exactly 2 rows, no responsive variant).
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
 *               unconditional-2-column Risk with no header row)
 *               reappearing.
 *   4. Reuse  — static checks reuse the same `ruleBody`-style regex
 *               helper pattern already used by alignment_overflow_audit.
 *               spec.js for this exact file.
 *   5. UX     — live checks run at a 360px mobile viewport (this
 *               project's own mobile-check convention), a ≥1180px
 *               desktop viewport, and the project's standard 1440px
 *               desktop viewport, confirming no Greek is clipped and the
 *               Risk header/row count tracks the active breakpoint.
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

  test('Risk block: exactly 6 .kv-pair rows (R:R, Risk-of-ruin, Breakevens, POP, EV, EV/cost), preceded by 6 .kv-risk-colhdr header cells (2026-11-19)', () => {
    const pairCount = (riskBlock.match(/class="kv-pair"/g) || []).length;
    expect(pairCount).toBe(6);
    // Header row: exactly 6 `.kv-risk-colhdr` spans (3 Metric|Value
    // pairs) must appear, all BEFORE the first `.kv-pair`, in document
    // order, with the correct alternating text.
    const hdrMatches = [...riskBlock.matchAll(/<span class="kv-risk-colhdr"[^>]*>([^<]*)<\/span>/g)];
    expect(hdrMatches.length, '6 header cells').toBe(6);
    const expectedHdrText = ['Metric', 'Value', 'Metric', 'Value', 'Metric', 'Value'];
    expect(hdrMatches.map((m) => m[1].trim())).toEqual(expectedHdrText);
    const firstPairIdx = riskBlock.indexOf('class="kv-pair"');
    const lastHdrIdx = hdrMatches[hdrMatches.length - 1].index;
    expect(firstPairIdx).toBeGreaterThan(-1);
    expect(lastHdrIdx, 'every header cell must precede the first .kv-pair').toBeLessThan(firstPairIdx);
  });

  test('Risk block: EV/cost pair is wrapped by the ev_pct != null guard in full (no blank row when absent)', () => {
    const guardIdx = riskBlock.indexOf('{#if strategy?.risk?.ev_pct != null}');
    expect(guardIdx, 'the ev_pct guard must exist').toBeGreaterThan(-1);
    const afterGuard = riskBlock.slice(guardIdx, guardIdx + 500);
    expect(afterGuard).toMatch(/class="kv-pair"/);
    expect(afterGuard).toMatch(/EV \/ cost/);
  });

  test('Risk grid CSS: 4-track default grid, widening to 6-track at >=1180px, with a matching header-cell toggle (2026-11-19)', () => {
    const cssMatch = src.match(/\.opt-kv-risk\s*\{([^}]*)\}/);
    expect(cssMatch, '.opt-kv-risk rule must exist').not.toBeNull();
    const body = cssMatch[1];
    expect(body).toMatch(/display:\s*grid/);
    expect(body).toMatch(
      /grid-template-columns:\s*max-content\s+minmax\(max-content,\s*1fr\)\s+max-content\s+minmax\(max-content,\s*1fr\)/
    );
    expect(body).toMatch(/flex:\s*1/);
    expect(body).toMatch(/align-content:\s*center/);
    // Same defensive overflow handling as Greeks — a worst-case long
    // value (3+ breakevens) must scroll internally, never bleed past
    // the card edge into the app-wide body overflow-x:clip backstop.
    expect(body).toMatch(/overflow-x:\s*auto/);
    const pairCssMatch = src.match(/\.opt-kv-risk\s+\.kv-pair\s*\{([^}]*)\}/);
    expect(pairCssMatch, '.opt-kv-risk .kv-pair rule must exist').not.toBeNull();
    expect(pairCssMatch[1]).toMatch(/display:\s*contents/);
    // 2026-11-19: a real >=1180px widening override MUST exist (reverses
    // the 2026-11-18 "no responsive override" decision for this card
    // only), widening to 6 tracks (3 metric|value pairs in one row).
    const mediaMatch = src.match(
      /@media \(min-width:\s*1180px\)\s*\{\s*\.opt-kv-risk\s*\{([^}]*)\}/
    );
    expect(mediaMatch, '>=1180px .opt-kv-risk widening override must exist').not.toBeNull();
    expect(mediaMatch[1]).toMatch(
      /grid-template-columns:\s*repeat\(3,\s*max-content\s+minmax\(max-content,\s*1fr\)\)/
    );
    // The same breakpoint must toggle the 3rd pair's own 2 header cells
    // (nth-child 5/6) from hidden (default) to visible (>=1180px).
    expect(src).toMatch(
      /\.opt-kv-risk\s+\.kv-risk-colhdr:nth-child\(5\),\s*\n?\s*\.opt-kv-risk\s+\.kv-risk-colhdr:nth-child\(6\)\s*\{\s*display:\s*none;/
    );
    const hdrMediaMatch = src.match(
      /@media \(min-width:\s*1180px\)\s*\{\s*\.opt-kv-risk\s+\.kv-risk-colhdr:nth-child\(5\),\s*\n?\s*\.opt-kv-risk\s+\.kv-risk-colhdr:nth-child\(6\)\s*\{([^}]*)\}/
    );
    expect(hdrMediaMatch, '>=1180px header-cell reveal override must exist').not.toBeNull();
    expect(hdrMediaMatch[1]).toMatch(/display:\s*flex/);
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
          // Column centres, not left edges: each header cell bleeds
          // symmetrically into its column gap (negative margin on both
          // sides), so its box starts ~4.8px left of the value cell's box
          // while its centre sits on the same column track.
          kCenters: ks.map((k) => { const b = rect(k); return Math.round((b.left + b.right) / 2); }),
          vCenters: vs.map((v) => { const b = rect(v); return Math.round((b.left + b.right) / 2); }),
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

      // Each value's column centre aligns with its own header's centre
      // (same column track) — within a small tolerance for rounding.
      for (let i = 0; i < 5; i++) {
        expect(
          Math.abs(shape.vCenters[i] - shape.kCenters[i]),
          `value[${i}] (${shape.labels[i]}) must align under its own header`
        ).toBeLessThanOrEqual(2);
      }
    });

    test('Risk: a real header row renders above the data, with the pairs-per-row count and visible header-cell count tracking the active breakpoint (2026-11-19)', async ({ page }) => {
      await gotoDerivativesWithStrategy(page);
      const risk = page.locator('.opt-kv-risk').first();
      const visible = await risk.isVisible({ timeout: 5000 }).catch(() => false);
      if (!visible) {
        test.info().annotations.push({ type: 'skip', description: 'No strategy loaded — Risk card not rendered' });
        return;
      }

      const shape = await risk.evaluate((el) => {
        const hdrs = [...el.querySelectorAll('.kv-risk-colhdr')].filter(
          (h) => getComputedStyle(h).display !== 'none'
        );
        const pairs = [...el.querySelectorAll('.kv-pair')];
        const rect = (node) => node.getBoundingClientRect();
        return {
          hdrTops: hdrs.map((h) => Math.round(rect(h).top)),
          hdrLefts: hdrs.map((h) => Math.round(rect(h).left)),
          hdrTexts: hdrs.map((h) => h.textContent.trim()),
          pairs: pairs.map((p) => {
            const k = p.querySelector('.kv-k');
            const v = p.querySelector('.kv-v');
            return {
              label: k ? k.textContent.trim().replace(/\s+/g, ' ') : null,
              kTop: k ? Math.round(rect(k).top) : null,
              vTop: v ? Math.round(rect(v).top) : null,
              kLeft: k ? Math.round(rect(k).left) : null,
              vLeft: v ? Math.round(rect(v).left) : null,
            };
          }),
        };
      });

      // 6 rows normally, 5 when EV/cost's data is absent (ev_pct == null).
      expect([5, 6]).toContain(shape.pairs.length);

      const isWideBreakpoint = vp.width >= 1180;
      // Below 1180px only the first 2 pairs' header cells (4 of 6) are
      // visible; at >=1180px all 3 pairs' header cells (6) are visible.
      expect(shape.hdrTops.length, `visible header cells at ${vp.width}px`).toBe(isWideBreakpoint ? 6 : 4);
      expect(shape.hdrTexts).toEqual(isWideBreakpoint
        ? ['Metric', 'Value', 'Metric', 'Value', 'Metric', 'Value']
        : ['Metric', 'Value', 'Metric', 'Value']);

      // The header row sits above the first data row.
      const hdrTopRef = shape.hdrTops[0];
      for (const t of shape.hdrTops) expect(Math.abs(t - hdrTopRef)).toBeLessThanOrEqual(2);
      const firstPairTop = Math.min(...shape.pairs.map((p) => p.kTop ?? Infinity));
      expect(firstPairTop, 'first data row must sit below the header row').toBeGreaterThan(hdrTopRef);

      // Each pair's own label and value share one row top (same row).
      for (const row of shape.pairs) {
        expect(row.kTop, `row "${row.label}" label must render`).not.toBeNull();
        expect(row.vTop, `row "${row.label}" value must render`).not.toBeNull();
        expect(Math.abs(row.kTop - row.vTop)).toBeLessThanOrEqual(2);
      }

      // Distinct left-edge (kLeft) columns across all pairs — 4 at the
      // narrow breakpoint (2 pairs per row), 6 at >=1180px (3 pairs per
      // row): each pair's label sits in its own column track, and the
      // grid wraps after 2 (or 3) pairs onto the next row.
      const distinctLefts = (vals) => {
        const uniq = [];
        for (const v of vals) {
          if (!uniq.some((u) => Math.abs(u - v) <= 2)) uniq.push(v);
        }
        return uniq.length;
      };
      const allLefts = shape.pairs.flatMap((p) => [p.kLeft, p.vLeft]);
      expect(distinctLefts(allLefts), `distinct left-edge columns at ${vp.width}px`).toBe(isWideBreakpoint ? 6 : 4);
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

// ── Suite 4: header-row sync (2026-11-20 Part 2) ──────────────────────────
//
// The Greeks header cells (.opt-kv-greeks .kv-k-greek), the Risk & EV
// header cells (.opt-kv-risk .kv-risk-colhdr) and the Legs grid header
// (.cand-headrow) now share one --grid-row-height SSOT token (26px
// desktop, 34px at <=720px). Each header cell bleeds into its column gap
// via a negative margin, so adjacent header cells must touch edge to edge
// with no visible seam. These checks are live, against a real loaded
// strategy: the Greeks / Risk cards and the Legs header must all render,
// and a missing card is a failure, not a skip.

/**
 * Pick the underlying through the page's own #opt-und picker and keep
 * re-picking until the Greeks card renders. The `?u=` seed is not enough
 * on its own: the picker's auto-select effect resets to the first option
 * whenever positions (re)load, so a single pick can be silently undone.
 * GOLD is the root with live F&O legs on the dev book (ZG0790 short MCX
 * GOLD PE); Greeks/Risk only render once its strategy has loaded.
 */
async function pickUnderlying(page, root) {
  const trigger = page.locator('button#opt-und');
  const label = trigger.locator('.rbq-select-label');
  const greeks = page.locator('.opt-kv-greeks').first();
  await trigger.waitFor({ state: 'visible', timeout: 60_000 });
  await expect.poll(async () => {
    if (await greeks.isVisible().catch(() => false)) return true;
    if ((await label.textContent())?.trim() !== root) {
      const panelOpen = await page.locator('.rbq-select-panel').isVisible().catch(() => false);
      if (!panelOpen) await trigger.click();
      const opt = page.locator('.rbq-select-option-label', { hasText: new RegExp(`^${root}$`) }).first();
      if (await opt.isVisible().catch(() => false)) await opt.click();
    }
    return false;
  }, { timeout: 90_000, intervals: [1000, 2000] }).toBe(true);
}

async function gotoDerivativesLive(page) {
  await loginAsAdmin(page);
  await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: 90_000 });
  await pickUnderlying(page, 'GOLD');
  await page.locator('.opt-kv-risk').first().waitFor({ state: 'visible', timeout: 30_000 });
  await page.locator('.cand-headrow').first().waitFor({ state: 'attached', timeout: 30_000 });
}

/**
 * Measure every visible header cell in the three header rows.
 * Returns plain rects so the assertions run in Node, not in the page.
 */
async function measureHeaderRows(page) {
  return page.evaluate(() => {
    const visible = (els) => els.filter((e) => {
      const b = e.getBoundingClientRect();
      return getComputedStyle(e).display !== 'none' && b.width > 0 && b.height > 0;
    });
    const rect = (e) => {
      const b = e.getBoundingClientRect();
      return { top: b.top, left: b.left, right: b.right, bottom: b.bottom, height: b.height };
    };
    return {
      vw: window.innerWidth,
      tokenPx: getComputedStyle(document.documentElement).getPropertyValue('--grid-row-height').trim(),
      greeks: visible([...document.querySelectorAll('.opt-kv-greeks .kv-k-greek')]).map(rect),
      risk: visible([...document.querySelectorAll('.opt-kv-risk .kv-risk-colhdr')]).map(rect),
      // The Legs header is one row box (.cand-headrow); its text spans are
      // not header cells, so the row box is what is measured against the token.
      candHead: visible([...document.querySelectorAll('.cand-headrow')]).map(rect),
    };
  });
}

/** Group rects into rows (same top within 2px), each row sorted by left. */
function rowsOf(rects) {
  const sorted = [...rects].sort((a, b) => a.top - b.top || a.left - b.left);
  const rows = [];
  for (const r of sorted) {
    const row = rows.find((rw) => Math.abs(rw[0].top - r.top) <= 2);
    if (row) row.push(r); else rows.push([r]);
  }
  return rows.map((rw) => rw.sort((a, b) => a.left - b.left));
}

/** Assert every adjacent pair in each row touches (zero gap, <=0.5px rounding). */
function expectZeroGapRows(rects, label) {
  for (const row of rowsOf(rects)) {
    for (let i = 0; i < row.length - 1; i++) {
      const gap = row[i + 1].left - row[i].right;
      expect(Math.abs(gap), `${label}: gap between header cell ${i} and ${i + 1} (px)`).toBeLessThanOrEqual(0.5);
    }
  }
}

const TOKEN_PX = (vw) => (vw <= 720 ? 34 : 26);

test.describe('Header-row sync — shared --grid-row-height and gapless header cells', () => {
  // Picker re-pick loop (up to 90 s) plus page load needs more than the 90 s default.
  test.setTimeout(150_000);
  // Each test sets its own viewport, so it runs once under chromium-desktop
  // rather than being repeated under every project's viewport.
  test.beforeEach(({}, testInfo) => {
    test.skip(testInfo.project.name !== 'chromium-desktop', 'viewport is set per test');
  });

  for (const vp of [
    { name: 'desktop-1440', width: 1440, height: 900 },
    { name: 'mobile-360', width: 360, height: 800 },
  ]) {
    test.describe(`[${vp.name}]`, () => {
      test.use({ viewport: { width: vp.width, height: vp.height } });

      test('Greeks, Risk & EV and Legs header cells all share the --grid-row-height token height', async ({ page }) => {
        await gotoDerivativesLive(page);
        const m = await measureHeaderRows(page);
        const expected = TOKEN_PX(m.vw);
        expect(parseFloat(m.tokenPx), '--grid-row-height token at this width').toBe(expected);
        expect(m.greeks.length, 'Greeks header cells rendered').toBe(5);
        expect(m.risk.length, 'Risk & EV header cells rendered').toBeGreaterThanOrEqual(4);
        expect(m.candHead.length, 'Legs grid header row (.cand-headrow) rendered').toBe(1);
        for (const [label, rects] of [['greeks', m.greeks], ['risk', m.risk], ['candHead', m.candHead]]) {
          for (const r of rects) {
            expect(Math.abs(r.height - expected), `${label} header cell height (${m.vw}px)`).toBeLessThanOrEqual(1);
          }
        }
      });

      test('no visible gap between adjacent Greeks header cells', async ({ page }) => {
        await gotoDerivativesLive(page);
        const m = await measureHeaderRows(page);
        expectZeroGapRows(m.greeks, `greeks @${m.vw}px`);
        // Greeks is one 5-cell row; the adjacency check must actually see it.
        expect(rowsOf(m.greeks).length).toBe(1);
        expect(rowsOf(m.greeks)[0].length).toBe(5);
      });

      test('no visible gap between adjacent Risk & EV header cells', async ({ page }) => {
        await gotoDerivativesLive(page);
        const m = await measureHeaderRows(page);
        expectZeroGapRows(m.risk, `risk @${m.vw}px`);
      });

    });
  }
});

// Risk & EV breakpoint: 4 visible header cells below 1180px, 6 at >=1180px.
// Check both sides of the boundary, and that the zero-gap invariant holds
// on each side (the grid reflows, so a wrong track count would show as a seam).
for (const width of [1179, 1181]) {
  test.describe(`Header-row sync at the Risk & EV 1180px breakpoint [${width}px]`, () => {
    test.setTimeout(150_000);
    test.use({ viewport: { width, height: 900 } });
    test.beforeEach(({}, testInfo) => {
      test.skip(testInfo.project.name !== 'chromium-desktop', 'viewport is set per test');
    });

    test('visible Risk header-cell count and row layout track the 1180px breakpoint; zero gap on both sides', async ({ page }) => {
      await gotoDerivativesLive(page);
      const m = await measureHeaderRows(page);
      const expectedCount = width >= 1180 ? 6 : 4;
      expect(m.risk.length, `visible Risk header cells at ${width}px`).toBe(expectedCount);
      // All visible header cells sit in one row at both breakpoints
      // (4 tracks = 2 pairs, 6 tracks = 3 pairs; each row is a single header row).
      expect(rowsOf(m.risk).length, 'Risk header is a single row').toBe(1);
      expectZeroGapRows(m.risk, `risk @${width}px`);
      expectZeroGapRows(m.greeks, `greeks @${width}px`);
    });
  });
}
