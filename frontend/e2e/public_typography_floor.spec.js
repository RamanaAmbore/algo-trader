// Public typography floor — B2 (2026-09)
//
// Raises the smallest label/meta text on (public) pages + the investor
// portal to a 0.7rem (11.2px) legibility floor — was as small as 0.4rem
// (~6.4px) on the nav tagline and 0.55-0.68rem elsewhere. Confirmed-worst
// named in the plan: .pub-brand-tagline (nav) and .stat-label (/about);
// broadened to every other sub-floor label/meta selector on the same
// pages (.trust-lbl, .term-lbl, .term-note, .closer-disclaimer,
// .prose-section-label, .faq-zoom-hint, and the investor page's
// .ip-tag/.ip-hero-lbl/.ip-hero-asof/.ip-tile-lbl/.ip-statement-hint).
//
// Intentionally NOT raised (small UI control/badge affordances, not
// data/credibility labels): .pub-user-role (role chip), .pub-brand-sub/
// .pub-brand-name (fixed-size brand wordmark), signin's .pw-toggle
// (show/hide password control).
//
// Five quality dimensions (feedback_test_dimensions.md):
//   1. SSOT    — live DOM computed-style sweep against the actual pages,
//                not a hardcoded list of selectors (a sweep test is
//                sturdier than checking class-by-class — a future new
//                label added below the floor fails here too).
//   2. Perf    — no login needed (all public pages).
//   3. Stale   — n/a (new coverage).
//   4. Reuse   — same sweep helper run across every touched page.
//   5. UX      — desktop (1400px) sweep + mobile (360px, matches the
//                mobile-portrait project's viewport) no-overflow check,
//                since the tagline bump widens the brand block.

import { test, expect } from '@playwright/test';

const PAGES = ['/', '/about', '/faq'];
const FLOOR_PX = 11.2; // 0.7rem at the default 16px root font-size

/**
 * Walks every element with visible, non-whitespace direct text and
 * returns any whose computed font-size falls under FLOOR_PX.
 * @param {import('@playwright/test').Page} page
 */
async function sweepSmallText(page, floorPx) {
  return page.evaluate((floor) => {
    const offenders = [];
    const all = document.querySelectorAll('body *');
    for (const el of all) {
      // Only elements with their OWN direct text (not just descendant
      // text) — avoids double-counting container ancestors.
      const hasDirectText = Array.from(el.childNodes).some(
        (n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim().length > 0
      );
      if (!hasDirectText) continue;
      // Intentionally exempt: fixed-size brand wordmark spans (a
      // stylized logotype, not a data/credibility label — same
      // reasoning as the file header comment) and the role-chip badge.
      const cls = typeof el.className === 'string' ? el.className : '';
      if (/\b(pub-brand-name|pub-brand-sub|pub-user-role)\b/.test(cls)) continue;
      const style = getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden') continue;
      // getComputedStyle only reports the ELEMENT'S OWN display/visibility —
      // it does NOT reflect an ancestor's `display: none` (e.g. the mobile
      // nav bar's `md:hidden` Tailwind class at desktop widths). A child
      // nested inside such an ancestor still reports its own declared
      // `display: block`/`visibility: visible`, producing a false positive
      // here even though nothing is actually rendered on screen. getClientRects()
      // reflects TRUE rendered visibility (empty when any ancestor is
      // display:none), so use that as the real gate.
      if (el.getClientRects().length === 0) continue;
      const size = parseFloat(style.fontSize);
      if (size < floor) {
        offenders.push({
          tag: el.tagName,
          className: typeof el.className === 'string' ? el.className : '',
          text: el.textContent.trim().slice(0, 40),
          fontSize: size,
        });
      }
    }
    return offenders;
  }, floorPx);
}

for (const path of PAGES) {
  test(`no visible text under ${FLOOR_PX}px on ${path} (desktop)`, async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await page.goto(path, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(500);
    const offenders = await sweepSmallText(page, FLOOR_PX);
    expect(offenders, `sub-floor text found on ${path}: ${JSON.stringify(offenders, null, 2)}`).toEqual([]);
  });
}

test('mobile (360px) — brand tagline bump does not cause horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(500);
  const { scrollWidth, clientWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(scrollWidth, `scrollWidth ${scrollWidth} must not exceed clientWidth ${clientWidth}`)
    .toBeLessThanOrEqual(clientWidth + 1); // +1px rounding tolerance
});

test('mobile (360px) — brand tagline renders at the 0.52rem exempt size, single line', async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(500);
  const metrics = await page.evaluate(() => {
    const el = document.querySelector('.pub-brand-mobile .pub-brand-tagline');
    if (!el) return null;
    return {
      fontSize: parseFloat(getComputedStyle(el).fontSize),
      lineCount: el.getClientRects().length,
    };
  });
  expect(metrics, '.pub-brand-mobile .pub-brand-tagline not found').not.toBeNull();
  // 0.52rem at the default 16px root = 8.32px. This pins the exempt
  // mobile-only value so a future regen against the wrong target
  // (e.g. dev.ramboq.com instead of the local edit) fails loudly
  // instead of silently passing the overflow-only check above.
  expect(metrics.fontSize, `expected 0.52rem (8.32px), got ${metrics.fontSize}px`).toBeCloseTo(8.32, 1);
  expect(metrics.lineCount, 'tagline must render on a single line, not wrap').toBe(1);
});

test('mobile (390px) — /about and /faq also fit without horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const path of ['/about', '/faq']) {
    await page.goto(path, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(500);
    const { scrollWidth, clientWidth } = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(scrollWidth, `${path}: scrollWidth ${scrollWidth} must not exceed clientWidth ${clientWidth}`)
      .toBeLessThanOrEqual(clientWidth + 1);
  }
});

test('stale code — .ag-theme-ramboq / investor page no longer reference the unloaded Inter font', async () => {
  const { readFileSync } = await import('fs');
  const css = readFileSync(new URL('../src/app.css', import.meta.url).pathname, 'utf8');
  const investor = readFileSync(
    new URL('../src/routes/investor/[token]/+page.svelte', import.meta.url).pathname, 'utf8'
  );
  expect(css, '.ag-theme-ramboq --ag-font-family must not reference Inter').not.toMatch(
    /--ag-font-family:\s*'Inter'/
  );
  expect(investor, 'investor page font-family must not reference Inter').not.toMatch(
    /font-family:\s*'Inter'/
  );
});

test('diff proof — B2 does not touch global body/:root/.ag-theme-algo selectors', async () => {
  const { readFileSync } = await import('fs');
  const css = readFileSync(new URL('../src/app.css', import.meta.url).pathname, 'utf8');
  // The base body selector (app.css ~375-380, font-size: 0.8125rem) that
  // also drives every algo trading page must be untouched by B2.
  const bodyMatch = css.match(/(?:^|\n)\s*body\s*\{([\s\S]*?)\n\s*\}/);
  expect(bodyMatch, 'app.css must still have a bare body {} rule').not.toBeNull();
  expect(bodyMatch[1], 'body {} rule must still set font-size: 0.8125rem').toMatch(/font-size:\s*0\.8125rem/);
});
