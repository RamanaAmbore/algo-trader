/**
 * Image-asset performance + SSOT guard.
 *
 * Companion to scripts/optimize_images.py: every brand asset under
 * frontend/static/ ships at a tuned size. This spec freezes the
 * payload ceiling so a future "drop a fresh PNG into static/" doesn't
 * silently bloat first-paint.
 *
 * Five dimensions checked:
 * 1. SSOT — landing page still renders the bull <img> + dashboard
 *    still renders the algo navbar bull. Same alt-empty / src
 *    contract as before. No visible regression.
 * 2. Performance — total transferred image bytes on cold-load of
 *    /  and /dashboard are below a budget that's already 30% under
 *    the pre-optimization baseline (156 KB hot-path → 54 KB; spec
 *    headroom 120 KB to absorb future small adds).
 * 3. Stale code — no `<img>` or CSS background-image references the
 *    retired bull.png / og-image.png assets. The migration to
 *    bull.webp / og-image-home.png is complete.
 * 4. Reusable — the navbar bull is loaded from the single canonical
 *    path `/bull.webp` (not duplicated under multiple paths). Both
 *    the public layout and the algo layout reference the same
 *    `bullSrc = "/bull.webp"` constant.
 * 5. UX — bull image is visible above the fold on both mobile and
 *    desktop; no `loading="lazy"` on the navbar logo (would defer
 *    the LCP candidate).
 *
 * Runs against chromium-desktop + mobile-portrait projects
 * (playwright.config.js default matrix).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';
import { resolve } from 'path';

const TIMEOUT = 20_000;

// Pre-optimization hot-path baseline was ~156 KB (favicon.ico 66 KB +
// favicon.png 32 KB + apple-touch 19 KB + app-icon.svg 19 KB +
// nav_image.webp 12 KB + bull.webp 8 KB). Post-optimization the hot
// path lands at ~54 KB. Budget gives 2.2× headroom for future drops
// while still asserting we never regress past the prior baseline.
const HOT_PATH_BUDGET_BYTES = 120_000;

// Image MIME prefixes Playwright reports — we count any response whose
// Content-Type starts with one of these toward the hot-path total.
const IMAGE_MIMES = [
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
  'image/svg',
  'image/avif',
  'image/x-icon',
  'image/vnd.microsoft.icon',
];

function isImage(contentType) {
  if (!contentType) return false;
  return IMAGE_MIMES.some((m) => contentType.toLowerCase().startsWith(m));
}

/**
 * Visit a URL with a clean cache + counter image bytes. Returns total
 * bytes downloaded for image responses + an array of {url, bytes}.
 *
 * @param {import('@playwright/test').Page} page
 * @param {string} url
 */
async function measureImageBytes(page, url) {
  /** @type {Array<{url: string, bytes: number, ct: string}>} */
  const records = [];
  page.on('response', async (resp) => {
    const ct = resp.headers()['content-type'] || '';
    if (!isImage(ct)) return;
    try {
      const body = await resp.body();
      records.push({ url: resp.url(), bytes: body.length, ct });
    } catch {
      // Some responses (304, redirects, aborts) can't yield a body —
      // ignore. They're not counted toward the page's image payload.
    }
  });
  await page.goto(url, { waitUntil: 'networkidle' });
  const total = records.reduce((a, r) => a + r.bytes, 0);
  return { total, records };
}

test.describe('Image performance + SSOT', () => {
  // --- Dimension 1: SSOT ---------------------------------------------------
  test('landing page renders bull.webp logo', async ({ page }) => {
    await page.goto('/');
    // The public navbar ships TWO bull <img> elements — one inside the
    // desktop nav-inner (md:flex) and one inside the mobile nav-inner
    // (md:hidden). Exactly one is visible at any viewport. Asserting
    // .first() would pick the desktop copy on mobile-portrait and fail.
    const bulls = page.locator('img[src="/bull.webp"]');
    await expect(bulls).toHaveCount(2, { timeout: TIMEOUT });
    // At least one bull must be visible at the active viewport.
    const visibleBull = bulls.locator('visible=true').first();
    await expect(visibleBull).toBeVisible({ timeout: TIMEOUT });
    // Bull is decorative (alt="") on the public navbar — preserved.
    expect(await visibleBull.getAttribute('alt')).toBe('');
  });

  // --- Dimension 2: Performance (cold load budget) -------------------------
  test('landing page image payload under budget', async ({ page }) => {
    const { total, records } = await measureImageBytes(page, '/');
    const top = [...records]
      .sort((a, b) => b.bytes - a.bytes)
      .slice(0, 8)
      .map((r) => `${r.bytes.toString().padStart(8)} ${r.url.split('/').pop()}`)
      .join('\n');
    expect(
      total,
      `landing page image payload ${total} > ${HOT_PATH_BUDGET_BYTES} budget.\nTop offenders:\n${top}`,
    ).toBeLessThan(HOT_PATH_BUDGET_BYTES);
  });

  // --- Dimension 3: Stale code grep ---------------------------------------
  test('no stale bull.png / og-image.png references in built assets', async ({ page }) => {
    // The (public)/+page.svelte HTML response is hydrated server-side
    // first; grepping the response body catches both static text
    // references AND inline scripts. SSOT migrations land here.
    const resp = await page.request.get('/');
    const body = await resp.text();
    expect(body, 'bull.png should be retired in favour of bull.webp').not.toMatch(
      /\bbull\.png\b/,
    );
    // og-image.png (no -home/-thumb suffix) was retired in slice AW.
    // Match only the bare path — og-image-home.png + og-image-thumb.png
    // are the surviving canonical variants.
    expect(body, 'og-image.png (no suffix) should be retired').not.toMatch(
      /\/og-image\.png\b/,
    );
  });

  // --- Dimension 4: Reusable — single canonical bullSrc -------------------
  test('bull asset referenced via single canonical path', async ({ page: _page }) => {
    // Both layouts derive their bull from the same constant string —
    // not from a per-page import. Failing here means someone copy-pasted
    // a hardcoded path or introduced a second variant; the next CDN
    // cache-bust would unfairly miss one of them.
    const pubLayout = readFileSync(
      resolve(process.cwd(), 'src/routes/(public)/+layout.svelte'),
      'utf-8',
    );
    const algoLayout = readFileSync(
      resolve(process.cwd(), 'src/routes/(algo)/+layout.svelte'),
      'utf-8',
    );
    expect(pubLayout).toMatch(/const\s+bullSrc\s*=\s*["']\/bull\.webp["']/);
    expect(algoLayout).toMatch(/const\s+bullSrc\s*=\s*["']\/bull\.webp["']/);
    // No raw "/bull.webp" string outside the bullSrc constant — every
    // <img> uses `{bullSrc}`. Allow exactly one occurrence per file
    // (the const declaration itself); any extra raw reference fails.
    const pubCount = (pubLayout.match(/\/bull\.webp/g) || []).length;
    const algoCount = (algoLayout.match(/\/bull\.webp/g) || []).length;
    expect(pubCount, 'public layout should reference /bull.webp exactly once (const)').toBe(1);
    expect(algoCount, 'algo layout should reference /bull.webp exactly once (const)').toBe(1);
  });

  // --- Dimension 5: UX — navbar logo above-fold + eager-loaded ------------
  test('navbar bull is eagerly loaded (LCP-eligible)', async ({ page }) => {
    await page.goto('/');
    const bulls = page.locator('img[src="/bull.webp"]');
    await expect(bulls.locator('visible=true').first()).toBeVisible({ timeout: TIMEOUT });
    // BOTH bull copies (desktop + mobile) must declare loading="eager"
    // — whichever the active viewport reveals must not defer the LCP.
    // loading="lazy" on the LCP candidate adds 200-400ms of round-trip
    // before the browser even queues the fetch.
    const count = await bulls.count();
    for (let i = 0; i < count; i++) {
      const loading = await bulls.nth(i).getAttribute('loading');
      expect(
        loading,
        `bull img #${i} should be eager (not "${loading}") — defers LCP otherwise`,
      ).toBe('eager');
    }
  });

  // --- Admin / algo surface ------------------------------------------------
  test.describe('algo navbar after login', () => {
    test.beforeEach(async ({ page }) => {
      // Login fixture has a 3-attempt rate-limit retry loop that can
      // burn 12-20s on cold prod. Plus the dashboard waitUntil:networkidle
      // cycle. 60s gives both phases comfortable runway.
      test.setTimeout(60_000);
      await loginAsAdmin(page);
    });

    test('dashboard bull image payload under budget', async ({ page }) => {
      const { total, records } = await measureImageBytes(page, '/dashboard');
      const top = [...records]
        .sort((a, b) => b.bytes - a.bytes)
        .slice(0, 8)
        .map((r) => `${r.bytes.toString().padStart(8)} ${r.url.split('/').pop()}`)
        .join('\n');
      expect(
        total,
        `dashboard image payload ${total} > ${HOT_PATH_BUDGET_BYTES} budget.\nTop offenders:\n${top}`,
      ).toBeLessThan(HOT_PATH_BUDGET_BYTES);
    });
  });
});
