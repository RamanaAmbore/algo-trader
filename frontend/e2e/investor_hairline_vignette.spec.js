// Investor portal — B8 desktop side-margin decoration
//
// Two pieces, both computed-style-verified (not just visual eyeballing):
//   (a) 1px champagne-gold hairlines flanking the 920px content column,
//       desktop-only (>=1080px — comfortably above the strict 968px
//       minimum needed to avoid overlapping content at in-between
//       desktop widths).
//   (b) a radial vignette on the body background — flat cream across
//       the content column, shifting toward a deeper cream/champagne
//       tone in the margin band.
//
// Five quality dimensions (feedback_test_dimensions.md):
//   1. SSOT    — reads the actual rendered ::before/::after computed
//                style and the actual body background-image string, not
//                a hardcoded assumption about what "should" render.
//   2. Perf    — reuses the same API-mock fixture as investor_portal_
//                smoke.spec.js, no login needed.
//   3. Stale   — n/a (new surface).
//   4. Reuse   — same mockInvestorApi() pattern as the smoke spec.
//   5. UX      — asserts desktop (1400px) renders the decoration AND
//                mobile (390px) does not — and that the vignette's flat
//                zone mathematically covers the content column's actual
//                half-width, so it can't have regressed the existing
//                contrast-passing content area.

import { test, expect } from '@playwright/test';

/** @param {import('@playwright/test').Page} page */
async function mockInvestorApi(page) {
  await page.route('**/api/investor/*/slice', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        display_name: 'Test Partner', share_pct: 4.25, contribution: 1000000,
        firm_nav: 42000000, nav_share: 1785000, pnl: 285000, pnl_pct: 0.19,
        day_delta_share: 12500, day_delta_share_pct: 0.007, as_of_date: '2026-09-26',
      }),
    })
  );
  await page.route('**/api/investor/*/history*', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ rows: [
        { as_of_date: '2026-09-01', firm_nav: 40000000, nav_share: 1700000, pnl: 200000 },
        { as_of_date: '2026-09-02', firm_nav: 40200000, nav_share: 1708500, pnl: 208500 },
      ] }),
    })
  );
}

test('desktop (1400px) — hairlines render, 1px, reuse the footer champagne color', async ({ page }) => {
  await page.setViewportSize({ width: 1400, height: 900 });
  await mockInvestorApi(page);
  await page.goto('/investor/mock-token-e2e', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(500);

  const before = await page.locator('.ip-page').evaluate((el) => {
    const s = getComputedStyle(el, '::before');
    return { content: s.content, width: s.width, background: s.backgroundColor, position: s.position };
  });
  const after = await page.locator('.ip-page').evaluate((el) => {
    const s = getComputedStyle(el, '::after');
    return { content: s.content, width: s.width, background: s.backgroundColor };
  });

  expect(before.content, 'before pseudo-element must render').not.toBe('none');
  expect(before.width).toBe('1px');
  expect(before.position).toBe('fixed');
  // rgba(200,168,75,0.45) — same value as .pub-footer's hairline separators.
  expect(before.background).toBe('rgba(200, 168, 75, 0.45)');
  expect(after.content, 'after pseudo-element must render').not.toBe('none');
  expect(after.width).toBe('1px');
  expect(after.background).toBe('rgba(200, 168, 75, 0.45)');
});

test('mobile (390px) — hairlines do not render (no side margin to decorate)', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockInvestorApi(page);
  await page.goto('/investor/mock-token-e2e', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(500);

  const before = await page.locator('.ip-page').evaluate((el) => getComputedStyle(el, '::before').content);
  const after = await page.locator('.ip-page').evaluate((el) => getComputedStyle(el, '::after').content);
  expect(before).toBe('none');
  expect(after).toBe('none');
});

test('desktop (1024px, below the 1080px gate) — hairlines still do not render', async ({ page }) => {
  // 1024px content (920px) + 2×24px offset = 968px minimum, but the
  // gate gate is 1080px specifically so lines never collide with
  // content at in-between widths like this one.
  await page.setViewportSize({ width: 1024, height: 900 });
  await mockInvestorApi(page);
  await page.goto('/investor/mock-token-e2e', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(500);

  const before = await page.locator('.ip-page').evaluate((el) => getComputedStyle(el, '::before').content);
  expect(before).toBe('none');
});

test('vignette — body background-image is a radial-gradient whose flat zone covers the content column', async ({ page }) => {
  await page.setViewportSize({ width: 1400, height: 900 });
  await mockInvestorApi(page);
  await page.goto('/investor/mock-token-e2e', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(500);

  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundImage);
  expect(bg, `body backgroundImage: ${bg}`).toMatch(/radial-gradient/);

  // Parse "radial-gradient(<shape/size> at center, <stop1>, <stop2>, ...)"
  // and confirm the flat-cream zone (0% -> first non-cream stop) covers
  // at least the content column's half-width (920px / 2 = 460px), so the
  // vignette provably can't touch the readable content area — this is
  // the "doesn't regress the existing contrast-passing content area"
  // requirement, checked mathematically rather than by eye.
  const widthMatch = bg.match(/radial-gradient\(\s*([\d.]+)px/);
  expect(widthMatch, `could not parse radial-gradient ellipse width: ${bg}`).not.toBeNull();
  const ellipseWidthPx = parseFloat(widthMatch[1]);

  // Each stop is "rgb(r, g, b) NN%" — match color+percent as one unit so
  // the commas INSIDE rgb(...) don't get mistaken for stop separators.
  const stopRe = /(rgba?\([^)]+\))\s*(\d+)%/g;
  const stops = [...bg.matchAll(stopRe)].map(([, color, pct]) => ({ color, pct: parseFloat(pct) }));
  expect(stops.length, `expected 4 color stops, got ${stops.length}: ${bg}`).toBe(4);

  const [stop0, stop1] = stops;
  const flatZonePx = (ellipseWidthPx * stop1.pct) / 100;
  expect(flatZonePx, `flat zone ${flatZonePx}px must cover the 460px content half-width`)
    .toBeGreaterThanOrEqual(460);
  // The first two stops must be the SAME flat cream color (0% and the
  // flat-zone boundary) — confirms the center/content area truly stays
  // "as-is", not a smooth gradient starting immediately from center.
  expect(stop0.color).toBe(stop1.color);
});
