/**
 * Brand assets — favicons, og-image, navbar bull, meta tags.
 *
 * The static brand bundle lives under frontend/static/. Each of the
 * canonical assets must serve 200 with the right MIME, and the
 * navbar's <img src="/bull.png"> on the public site must carry the
 * champagne drop-shadow (rgba(200,168,75,*)) — NOT the algo-amber
 * variant (rgba(251,191,36,*)).
 *
 * The Svelte route `(public)/+page.svelte` is what renders at `/`,
 * and includes <meta property="og:image"> pointing at og-image.png.
 * After hydration, the (public) layout's nav bar mounts with the
 * bull.png. We assert against the post-hydration DOM.
 */

import { test, expect } from '@playwright/test';

const TIMEOUT = 20_000;

// Note: vite dev returns empty Content-Type for .ico (known quirk);
// production nginx serves image/x-icon. Accept either by allowing
// empty content-type to pass for .ico only.
const STATIC_OK = [
  { path: '/favicon.ico',      mime: /image\/(x-icon|vnd\.microsoft\.icon)|^$/ },
  { path: '/favicon.png',      mime: /image\/png/ },
  { path: '/og-image.png',     mime: /image\/png/ },
  { path: '/og-image.svg',     mime: /image\/svg\+xml/ },
  { path: '/app-icon-192.png', mime: /image\/png/ },
  { path: '/app-icon-512.png', mime: /image\/png/ },
  { path: '/logo.png',         mime: /image\/png/ },
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

  test('navbar bull.png on / has champagne drop-shadow + og:image meta', async ({ page }) => {
    await page.goto('/');

    // The home route renders prerendered SSR content first; SvelteKit
    // hydrates the (public) layout after. Wait for the bull image to
    // mount.
    const bull = page.locator('img[src="/bull.png"]').first();
    await expect(bull).toBeVisible({ timeout: TIMEOUT });

    // Read the inline style attribute and assert the champagne hue
    // shows up while the algo amber does NOT.
    const styleAttr = (await bull.getAttribute('style')) || '';
    expect(styleAttr, `bull style: ${styleAttr}`).toContain('drop-shadow');
    expect(styleAttr).toContain('rgba(200,168,75');
    expect(styleAttr).not.toContain('rgba(251,191,36');

    // og:image meta tag — the (public)/+page.svelte head block lands
    // it pointing at og-image.png (raster — WhatsApp/Twitter previews
    // don't render SVG OG images reliably).
    const og = await page.locator('meta[property="og:image"]').first().getAttribute('content');
    expect(og || '', 'og:image meta missing').toMatch(/og-image\.png$/);
  });
});
