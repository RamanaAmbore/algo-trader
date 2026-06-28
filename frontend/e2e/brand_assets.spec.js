/**
 * Brand assets — favicons, og-image, navbar bull, meta tags.
 *
 * The static brand bundle lives under frontend/static/. Each of the
 * canonical assets must serve 200 with the right MIME, and the
 * navbar's <img src="/bull.webp"> on the public site must carry the
 * champagne drop-shadow (rgba(200,168,75,*)) — NOT the algo-amber
 * variant (rgba(251,191,36,*)).
 *
 * The Svelte route `(public)/+page.svelte` is what renders at `/`,
 * and includes <meta property="og:image"> pointing at og-image-home.png.
 * After hydration, the (public) layout's nav bar mounts with bull.webp.
 * We assert against the post-hydration DOM.
 */

import { test, expect } from '@playwright/test';

const TIMEOUT = 20_000;

// Note: vite dev returns empty Content-Type for .ico (known quirk);
// production nginx serves image/x-icon. Accept either by allowing
// empty content-type to pass for .ico only.
const STATIC_OK = [
  { path: '/favicon.ico',      mime: /image\/(x-icon|vnd\.microsoft\.icon)|^$/ },
  { path: '/favicon.png',      mime: /image\/png/ },
  // og-image-card.{png,svg} retired in slice AW — was the unreferenced
  // "subpages" variant; the canonical 1200×630 share image is
  // og-image-home.* which every public page references via og:image.
  { path: '/og-image-home.png',     mime: /image\/png/ },
  { path: '/og-image-home.svg',     mime: /image\/svg\+xml/ },
  { path: '/og-image-thumb.png',    mime: /image\/png/ },
  { path: '/app-icon-192.png', mime: /image\/png/ },
  { path: '/app-icon-512.png', mime: /image\/png/ },
];

test.describe('Brand assets', () => {
  for (const asset of STATIC_OK) {
    test(`GET ${asset.path} → 200 ${String(asset.mime)}`, async ({ page }) => {
      const r = await page.request.get(asset.path);
      expect(r.status(), `expected 200 for ${asset.path}, got ${r.status()}`).toBe(200);
      const ct = r.headers()['content-type'] || '';
      expect(ct, `expected ${asset.mime} for ${asset.path}, got ${ct}`).toMatch(asset.mime);
    });
  }

  test('navbar bull.webp on / has champagne drop-shadow + og:image meta', async ({ page }) => {
    await page.goto('/');

    // The home route renders prerendered SSR content first; SvelteKit
    // hydrates the (public) layout after. Wait for the bull image to
    // mount. The bull asset migrated from .png to .webp in slice F
    // (2026-06) — same drop-shadow contract, smaller wire footprint.
    //
    // The public navbar renders TWO bull <img>s — desktop (md:flex)
    // and mobile (md:hidden). Exactly one is visible at any viewport,
    // so we select via the visible-pseudo rather than .first().
    const bulls = page.locator('img[src="/bull.webp"]');
    const bull = bulls.locator('visible=true').first();
    await expect(bull).toBeVisible({ timeout: TIMEOUT });

    // Read the inline style attribute and assert the champagne hue
    // shows up while the algo amber does NOT.
    const styleAttr = (await bull.getAttribute('style')) || '';
    expect(styleAttr, `bull style: ${styleAttr}`).toContain('drop-shadow');
    expect(styleAttr).toContain('rgba(200,168,75');
    expect(styleAttr).not.toContain('rgba(251,191,36');

    // og:image meta tag — the (public)/+page.svelte head block lands
    // it pointing at og-image-home.png (raster — WhatsApp/Twitter
    // previews don't render SVG OG images reliably). Canonical share
    // image renamed from og-image.png to og-image-home.png in slice AW.
    const og = await page.locator('meta[property="og:image"]').first().getAttribute('content');
    expect(og || '', 'og:image meta missing').toMatch(/og-image-home\.png/);
  });
});
