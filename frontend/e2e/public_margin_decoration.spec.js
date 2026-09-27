// (public)/+layout.svelte — B8 desktop side-margin decoration, extended
// from /investor/[token] (commit 94cfa193) to every route under
// (public)/ per explicit operator confirmation: "this should apply to
// ALL public pages, not just the investor portal."
//
// Centralized in the shared layout (.pub-viewport) rather than per-page,
// since every (public)/ route renders inside the same 1280px .pub-card
// frame — one implementation, not 6+ copies that could drift. Re-derived
// (not copy-pasted) geometry for the 1280px frame + the layout's extra
// position:fixed accent-top/bottom strips (see the CSS comment above
// `@media (min-width: 1440px)` in (public)/+layout.svelte for the math).
//
// Color/offset tokens (--b8-hairline-color, --b8-margin-offset) are
// shared with the investor page via app.css .card-theme-cream — this
// spec checks the SAME literal values the investor spec checks, so a
// drift in either surface's token resolution fails both suites.
//
// Five quality dimensions (feedback_test_dimensions.md):
//   1. SSOT    — reads the actual rendered ::before/::after computed
//                style and the actual .pub-viewport background-image
//                string, not a hardcoded assumption.
//   2. Perf    — no API mocking needed (about/faq/contact are static);
//                background-attachment:fixed is asserted to be gated
//                OFF below 1440px (no mobile scroll-repaint cost).
//   3. Stale   — n/a (new coverage).
//   4. Reuse   — same token values as investor_hairline_vignette.spec.js;
//                covers 3 of the 6+ (public)/ routes as a regression
//                trip-wire for the shared layout, not an exhaustive
//                per-page sweep.
//   5. UX      — asserts desktop (1600px, comfortably above the 1440px
//                gate) renders on 3 different pages, mobile (390px) does
//                not, and an in-between desktop width (1366px, below the
//                gate) does not — matching the investor page's own
//                below-gate check.

import { test, expect } from '@playwright/test';

const DESKTOP_PAGES = ['/about', '/faq', '/contact'];

for (const path of DESKTOP_PAGES) {
  test(`desktop (1600px) — ${path} renders B8 hairlines + vignette`, async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await page.goto(path, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(300);

    const before = await page.locator('.pub-viewport').evaluate((el) => {
      const s = getComputedStyle(el, '::before');
      return { content: s.content, width: s.width, background: s.backgroundColor, position: s.position };
    });
    const after = await page.locator('.pub-viewport').evaluate((el) => {
      const s = getComputedStyle(el, '::after');
      return { content: s.content, width: s.width, background: s.backgroundColor };
    });

    expect(before.content, `${path}: before pseudo-element must render`).not.toBe('none');
    expect(before.width).toBe('1px');
    expect(before.position).toBe('fixed');
    // Same token value as investor page's hairline — rgba(200,168,75,0.45).
    expect(before.background).toBe('rgba(200, 168, 75, 0.45)');
    expect(after.content, `${path}: after pseudo-element must render`).not.toBe('none');
    expect(after.width).toBe('1px');
    expect(after.background).toBe('rgba(200, 168, 75, 0.45)');

    // Hairline must sit clear of the 1280px .pub-card frame, not overlap
    // it. Scoped to the layout's own direct-child .pub-card — /faq has
    // an unrelated nested element that also carries a "pub-card" class
    // from a page-local component, so a bare `.pub-card` locator is
    // ambiguous on that route.
    const cardBox = await page.locator('.pub-viewport > .pub-card').boundingBox();
    const beforeLeft = await page.locator('.pub-viewport').evaluate((el) => {
      const s = getComputedStyle(el, '::before');
      return parseFloat(s.left);
    });
    expect(cardBox, `${path}: .pub-card must be measurable`).not.toBeNull();
    expect(beforeLeft, `${path}: hairline must be outside the card's left edge`)
      .toBeLessThan(cardBox.x - 15);

    // Vignette — .pub-viewport background-image carries a radial-gradient
    // layer alongside the pre-existing diagonal hatch.
    const bg = await page.locator('.pub-viewport').evaluate((el) => getComputedStyle(el).backgroundImage);
    expect(bg, `${path}: backgroundImage: ${bg}`).toMatch(/radial-gradient/);
    expect(bg, `${path}: existing hatch must not be removed`).toMatch(/repeating-linear-gradient/);

    const stopRe = /(rgba?\([^)]+\)|#[0-9a-f]{3,6})\s*(\d+)%/gi;
    const stops = [...bg.matchAll(stopRe)].map(([, color, pct]) => ({ color, pct: parseFloat(pct) }));
    expect(stops.length, `${path}: expected 4 vignette color stops, got ${stops.length}: ${bg}`).toBe(4);
    const [stop0, stop1] = stops;
    expect(stop0.color).toBe(stop1.color);

    const widthMatch = bg.match(/radial-gradient\(\s*([\d.]+)px/);
    expect(widthMatch, `${path}: could not parse radial-gradient ellipse width: ${bg}`).not.toBeNull();
    const ellipseWidthPx = parseFloat(widthMatch[1]);
    const flatZonePx = (ellipseWidthPx * stop1.pct) / 100;
    // Content half-width is 640px (1280px .pub-card / 2).
    expect(flatZonePx, `${path}: flat zone ${flatZonePx}px must cover the 640px content half-width`)
      .toBeGreaterThanOrEqual(640);
  });
}

test('desktop (1366px, below the 1440px gate) — /about does not render B8 decoration', async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 900 });
  await page.goto('/about', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(300);

  const before = await page.locator('.pub-viewport').evaluate((el) => getComputedStyle(el, '::before').content);
  expect(before).toBe('none');

  const bg = await page.locator('.pub-viewport').evaluate((el) => getComputedStyle(el).backgroundImage);
  expect(bg, `below-gate backgroundImage must not carry the vignette: ${bg}`).not.toMatch(/radial-gradient/);
});

test('mobile (390px) — /about renders no B8 decoration (existing hatch only)', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/about', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(300);

  const before = await page.locator('.pub-viewport').evaluate((el) => getComputedStyle(el, '::before').content);
  const after = await page.locator('.pub-viewport').evaluate((el) => getComputedStyle(el, '::after').content);
  expect(before).toBe('none');
  expect(after).toBe('none');

  const attachment = await page.locator('.pub-viewport').evaluate((el) => getComputedStyle(el).backgroundAttachment);
  expect(attachment, 'mobile must not pay the fixed-attachment scroll-repaint cost').not.toBe('fixed');

  const bg = await page.locator('.pub-viewport').evaluate((el) => getComputedStyle(el).backgroundImage);
  expect(bg, `mobile backgroundImage must not carry the vignette: ${bg}`).not.toMatch(/radial-gradient/);
  expect(bg, 'mobile keeps the pre-existing diagonal hatch').toMatch(/repeating-linear-gradient/);
});
