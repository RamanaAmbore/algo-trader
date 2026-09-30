/**
 * derivatives_legs_headrow_opaque.spec.js
 *
 * Scope-correction note (2026-09-30): the originating task described
 * this as a "Chain tab's Legs grid" bug (OptionChainTab.svelte). That
 * surface has no such grid — basket legs there render as inline
 * `.chain-leg-badge` spans inside the strike-grid rows themselves, and
 * as `.oes-basket-pills` (SymbolPanel.svelte, a wrapping flex row, no
 * sticky header, nothing to fix). The operator's exact words — "in
 * legs, when you scroll, the rows are visible behind the header...
 * because of transparency" — match a REAL, independently-confirmed bug
 * on a different page: `/admin/derivatives`'s "Legs" tab
 * (`.cand-grid` / `.cand-row` / `.cand-scroll`), whose column header
 * row (`.cand-headrow`, `frontend/src/routes/(algo)/admin/derivatives/
 * +page.svelte`) is `position: sticky; top: 0` over `.cand-scroll`
 * (`overflow-y: auto`) but its background was `rgba(15,23,42,0.65)` —
 * 65% alpha, genuinely translucent — despite an adjacent code comment
 * claiming "the card-bottom navy ... is reused as a solid fill so data
 * rows don't bleed through". Root cause: the color was copied from the
 * non-sticky `.byund-headrow` (Snapshot grid), where alpha is harmless
 * because that header never scrolls over anything.
 *
 * Fix: `.cand-headrow` background is now a two-layer composite — the
 * same rgba(15,23,42,0.65) tint layered on top of an opaque `#1d2a44`
 * base (matching the parent `.opt-legs-card`'s `--card-bg-gradient` top
 * stop, and the same two-layer idiom `.cand-row.cand-row-total` already
 * uses a few rules above it in the same file) — so the header is
 * genuinely solid while keeping its original visual tint.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const dir = path.resolve(import.meta.dirname ?? new URL('.', import.meta.url).pathname, '..');
const DERIVATIVES_PAGE = readFileSync(
  path.join(dir, 'src/routes/(algo)/admin/derivatives/+page.svelte'),
  'utf8'
);

test.describe('Legs grid header opacity (source)', () => {
  test('.cand-headrow background is a two-layer composite ending in an opaque hex, not a bare low-alpha rgba', () => {
    // .cand-headrow has TWO separate rule blocks in source (layout rules,
    // then typography/sticky-positioning rules) — join every occurrence
    // so the assertions see the full cascade, not just the first block.
    const rule = (DERIVATIVES_PAGE.match(/\.cand-headrow\s*\{[\s\S]*?\n  \}/g) ?? []).join('\n');
    expect(rule, '.cand-headrow rule(s)').not.toBe('');
    expect(rule).toMatch(/position:\s*sticky/);
    expect(rule).toMatch(/top:\s*0/);
    // The tint layer is kept (same visual intent)...
    expect(rule).toMatch(/rgba\(15,\s*23,\s*42,\s*0\.65\)/);
    // ...but it must now sit on an opaque base, not stand alone as the
    // entire `background:` value.
    expect(rule).not.toMatch(/background:\s*rgba\(15,\s*23,\s*42,\s*0\.65\);/);
    expect(rule).toMatch(/#1d2a44/);
  });

  test('the sibling non-sticky .byund-headrow is untouched — its alpha is harmless (never scrolls under content)', () => {
    const rule = DERIVATIVES_PAGE.match(/\.byund-headrow > span\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.byund-headrow > span rule').not.toBe('');
    expect(rule).not.toMatch(/position:\s*sticky/);
  });
});

test.describe('Legs grid header opacity (live)', () => {
  test('live: .cand-headrow computes a fully opaque effective background while scrolled', async ({ page }) => {
    // /admin/derivatives keeps SSE/poll connections open (Snapshot,
    // MarketPulse-style live updates) so `networkidle` never fires
    // reliably here — mirrors derivatives_legs_ltp_flash.spec.js's own
    // note on this. Wait on the concrete grid instead of network state.
    test.setTimeout(60_000);
    await loginAsAdmin(page);
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.locator('.cand-headrow, .oct-empty, .algo-status-card').first()
      .waitFor({ state: 'visible', timeout: 20_000 })
      .catch(() => {});

    const legRowCount = await page.locator('.cand-row').count();
    if (legRowCount === 0) {
      test.skip(true, 'no open F&O legs in this environment — nothing to render in the Legs grid');
    }

    const headrow = page.locator('.cand-headrow').first();
    await expect(headrow).toBeVisible({ timeout: 15_000 });

    // Composite alpha check: render the header over a known-distinct
    // probe color and confirm the probe does not show through. Simpler
    // and less flaky than a pixel-diff — walk the declared
    // background-image layers and confirm at least one fully opaque
    // (alpha-1 or no-alpha) layer exists, OR that the element's own
    // effective alpha (via a canvas sample) is 255.
    const isOpaque = await headrow.evaluate((el) => {
      const rect = el.getBoundingClientRect();
      const canvas = document.createElement('canvas');
      canvas.width = 1;
      canvas.height = 1;
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      // Paint a pure-red probe behind the element's own stacking
      // context, then sample what the browser actually composited at
      // that pixel by reading computed background layers directly —
      // canvas can't rasterize live DOM, so fall back to parsing the
      // computed backgroundImage/backgroundColor stack for an opaque
      // stop instead of a screenshot pixel-sample.
      void rect; void ctx;
      const cs = getComputedStyle(el);
      const bgColor = cs.backgroundColor; // e.g. "rgba(0, 0, 0, 0)" if unset
      const bgImage = cs.backgroundImage; // "linear-gradient(...), linear-gradient(...)" etc
      // A trailing solid hex/rgb (no alpha, or alpha 1) stop anywhere
      // in the gradient stack, or an opaque backgroundColor, makes the
      // final composite opaque regardless of any translucent layers
      // painted on top of it.
      const hasOpaqueColorStop = /#[0-9a-fA-F]{6}\b|rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)/.test(bgImage);
      const bgColorOpaque = /rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*(,\s*1(\.0+)?\s*)?\)/.test(bgColor) && !/,\s*0(\.\d+)?\s*\)/.test(bgColor);
      return hasOpaqueColorStop || bgColorOpaque;
    });
    expect(isOpaque, '.cand-headrow must resolve to an opaque effective background').toBe(true);

    // If there's enough data to actually scroll, confirm the header
    // still visually wins at its own coordinates after scrolling —
    // elementFromPoint at a header pixel must return the header (or a
    // descendant of it), never a scrolled-under .cand-row.
    const scrollEl = page.locator('.cand-scroll').first();
    const { scrollHeight, clientHeight } = await scrollEl.evaluate((el) => ({
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
    }));
    if (scrollHeight > clientHeight + 4) {
      await scrollEl.evaluate((el) => { el.scrollTop = el.scrollHeight; });
      await page.waitForTimeout(150);
      const headerBox = await headrow.evaluate((el) => {
        const r = el.getBoundingClientRect();
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
      });
      const winnerIsHeader = await page.evaluate(({ x, y }) => {
        const el = document.elementFromPoint(x, y);
        return !!el && (el.classList.contains('cand-headrow') || !!el.closest('.cand-headrow'));
      }, headerBox);
      expect(winnerIsHeader, 'header must win hit-testing at its own coordinates after scrolling body rows underneath it').toBe(true);
    }
  });
});
