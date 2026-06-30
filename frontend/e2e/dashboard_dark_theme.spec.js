/**
 * dashboard_dark_theme.spec.js
 *
 * Validates the Bloomberg-style dark navy + amber-accent theme applied across
 * ALL (algo)/* routes (operator terminal) while the public /performance route
 * keeps the cream palette unchanged.
 *
 * Implementation: CSS custom properties set by layout wrapper class
 *   .card-theme-dark  → (algo)/+layout.svelte (.algo-viewport) — all algo routes
 *   .card-theme-cream → (public)/+layout.svelte (.pub-viewport) — all public routes
 *                     → (public)/performance/+page.svelte also has local wrap
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *  1. SSOT       — NavCard + PerformancePage read var(--card-bg) etc.
 *                  Resolved values match expected palette per context.
 *  2. Perf       — no extra XHR introduced; render within budget.
 *  3. Stale      — source-grep guard: no bare #faf7f0 / #f0ead8 /
 *                  rgba(212, 146, 12, ... remain in NavCard.svelte or
 *                  PerformancePage.svelte (all migrated to var() reads).
 *                  Also verifies card-theme-dark is on algo layout, not
 *                  the dashboard section directly (lifted to layout level).
 *  4. Reuse      — same component code, theme controlled by layout wrapper.
 *  5. UX desktop + mobile — multiple algo routes render dark; public
 *                  /performance renders cream. Cross-page nav: no bleed.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test frontend/e2e/dashboard_dark_theme.spec.js \
 *   --project=chromium-desktop --project=chromium-mobile --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'node:fs';
import * as path from 'node:path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

/* ── 3. Stale guard — source files must not contain bare hard-coded
   cream/champagne values. Run once (not per-browser project) so
   parallel runs don't double-count. ───────────────────────────────── */
test.describe('Stale: no hard-coded cream colors in migrated components', () => {
  // These are the cream/champagne values that must have been replaced
  // with var() reads in NavCard.svelte + PerformancePage.svelte.
  const BANNED_PATTERNS = [
    /#f0ead8\b/,           // NavCard old bg
    /#faf7f0\b/,           // perf-strategy old bg
    /#d4c89f\b/,           // NavCard old border
    /#e0d9cc\b/,           // panel divider + strategy border (old)
    /#c8a84b\b/,           // nav-panel-label old color
    /#0c1830\b/,           // nav-big old color
    /#4a5872\b/,           // nav-currency old color
    /#7a6b52\b/,           // nav-meta / nav-zero old color
    /#a89878\b/,           // nav-as-of old color
    /#1a6b3a\b/,           // nav-gain old color
    /#9b1c1c\b/,           // nav-loss old color
    /rgba\(26,\s*107,\s*58/,   // tick-flash up old
    /rgba\(155,\s*28,\s*28/,   // tick-flash down old
    /#ede8df\b/,           // skel-bar from old
    /#f5f0e8\b/,           // skel-bar to old
  ];

  const TARGET_FILES = [
    path.join(process.cwd(), 'src/lib/NavCard.svelte'),
    path.join(process.cwd(), 'src/lib/PerformancePage.svelte'),
  ];

  for (const filePath of TARGET_FILES) {
    const shortName = path.basename(filePath);
    test(`${shortName} — no bare cream/champagne hex values remain`, () => {
      let src;
      try {
        src = fs.readFileSync(filePath, 'utf-8');
      } catch {
        // File not found means we're running from a different cwd — skip.
        test.skip(true, `Could not read ${filePath}; check working directory`);
        return;
      }

      // Strip CSS comment blocks (/* ... */) so mentions in comments
      // (e.g. "was #f0ead8") don't trip the guard. The spec cares about
      // live value assignments, not documentation.
      let noComments = src.replace(/\/\*[\s\S]*?\*\//g, '');

      // Also strip var() fallback values — e.g. `var(--card-bg, #f0ead8)` is
      // the CORRECT migrated form. We only want to flag bare usages that are
      // NOT inside a var() fallback (e.g. `color: #f0ead8;` without wrapping).
      // Strategy: replace `var(--..., <fallback>)` patterns with a placeholder
      // before scanning, so the fallback value inside is excluded from the check.
      noComments = noComments.replace(/var\([^)]+\)/g, 'VAR_PLACEHOLDER');

      for (const pattern of BANNED_PATTERNS) {
        expect(
          pattern.test(noComments),
          `${shortName} still contains bare hard-coded value matching ${pattern} — wrap in var()`
        ).toBe(false);
      }
    });
  }
});

/* ── 3b. Stale: layout-level wrapper guard ───────────────────────────
   Verify the theme class moved from per-route section to the layout
   so every algo route inherits it automatically.                    */
test.describe('Stale: card-theme-dark on algo layout, not per-route section', () => {
  test('algo layout (.algo-viewport) carries card-theme-dark', () => {
    const layoutPath = path.join(
      process.cwd(),
      'src/routes/(algo)/+layout.svelte',
    );
    let src;
    try { src = fs.readFileSync(layoutPath, 'utf-8'); }
    catch { test.skip(true, 'Cannot read algo layout source'); return; }

    expect(src).toMatch(/class="algo-viewport card-theme-dark"/);
  });

  test('dashboard page section does NOT carry card-theme-dark directly', () => {
    const dashPage = path.join(
      process.cwd(),
      'src/routes/(algo)/dashboard/+page.svelte',
    );
    let src;
    try { src = fs.readFileSync(dashPage, 'utf-8'); }
    catch { test.skip(true, 'Cannot read dashboard page source'); return; }

    // cap-eq-tabbed section must NOT carry the class — layout handles it.
    expect(src).not.toMatch(/cap-eq-tabbed[^"]*card-theme-dark|card-theme-dark[^"]*cap-eq-tabbed/);
  });

  test('public layout (.pub-viewport) carries card-theme-cream', () => {
    const pubLayout = path.join(
      process.cwd(),
      'src/routes/(public)/+layout.svelte',
    );
    let src;
    try { src = fs.readFileSync(pubLayout, 'utf-8'); }
    catch { test.skip(true, 'Cannot read public layout source'); return; }

    expect(src).toMatch(/class="pub-viewport card-theme-cream"/);
  });
});

/* ── 4. Reuse: wrapper classes present on the right routes ───────────── */
test.describe('Reuse: wrapper class applied in route HTML', () => {
  test('public /performance page HTML wraps PerformancePage in card-theme-cream', () => {
    const perfPage = path.join(
      process.cwd(),
      'src/routes/(public)/performance/+page.svelte',
    );
    let src;
    try { src = fs.readFileSync(perfPage, 'utf-8'); }
    catch { test.skip(true, 'Cannot read performance page source'); return; }
    expect(src).toMatch(/class="card-theme-cream"/);
  });
});

/* ── 1 + 5. SSOT + UX: computed styles on real pages ─────────────────── */
// These tests navigate to live pages and check computed CSS variable values.
// They require a running dev server and valid credentials.

test.describe('UX desktop: dark theme on /dashboard', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test('algo-viewport has card-theme-dark and dark CSS vars cascade to section', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded', timeout: 30000 });

    // Wait for the NAV | Capital | Equity tabbed card to mount.
    const navCard = page.locator('section.cap-eq-tabbed');
    await expect(navCard).toBeVisible({ timeout: 15000 });

    // 4. Reuse guard — the algo-viewport ancestor carries the dark wrapper class.
    const algoViewport = page.locator('div.algo-viewport');
    await expect(algoViewport).toHaveClass(/card-theme-dark/);

    // 1. SSOT — --card-bg cascades from .algo-viewport to the section child.
    const cardBgVar = await navCard.evaluate((el) => {
      return getComputedStyle(el).getPropertyValue('--card-bg').trim();
    });
    // The dark token is "linear-gradient(180deg, #1d2a44 0%, #152033 100%)"
    expect(
      cardBgVar.includes('1d2a44') || cardBgVar.includes('152033') || cardBgVar.includes('linear-gradient'),
      `Expected dark gradient for --card-bg on /dashboard, got: "${cardBgVar}"`
    ).toBe(true);

    // --card-label-text must resolve to the amber-400 token (#fbbf24).
    const labelTextVar = await navCard.evaluate((el) => {
      return getComputedStyle(el).getPropertyValue('--card-label-text').trim();
    });
    expect(labelTextVar.replace(/\s/g, '')).toBe('#fbbf24');
  });
});

test.describe('UX mobile: dark theme on /dashboard', () => {
  test.use({ viewport: { width: 393, height: 851 } });

  test('algo-viewport renders dark on mobile viewport', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded', timeout: 30000 });

    const algoViewport = page.locator('div.algo-viewport');
    await expect(algoViewport).toHaveClass(/card-theme-dark/);

    const navCard = page.locator('section.cap-eq-tabbed');
    await expect(navCard).toBeVisible({ timeout: 15000 });
    const labelVar = await navCard.evaluate((el) => {
      return getComputedStyle(el).getPropertyValue('--card-label-text').trim();
    });
    expect(labelVar.replace(/\s/g, '')).toBe('#fbbf24');
  });
});

/* ── 1 + 5. Dark theme propagation across algo routes ──────────────────
   Verifies the layout-level wrapper cascades to at least 5 distinct
   algo routes beyond /dashboard.                                      */
test.describe('UX desktop: dark theme propagates to multiple algo routes', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  const ALGO_ROUTES = [
    '/dashboard',
    '/pulse',
    '/admin/derivatives',
    '/orders',
    '/admin/history',
  ];

  for (const route of ALGO_ROUTES) {
    test(`${route} — algo-viewport has card-theme-dark and amber vars cascade`, async ({ page }) => {
      await loginAsAdmin(page);
      await page.goto(`${BASE}${route}`, { waitUntil: 'domcontentloaded', timeout: 30000 });

      const algoViewport = page.locator('div.algo-viewport');
      await expect(algoViewport).toBeVisible({ timeout: 15000 });
      await expect(algoViewport).toHaveClass(/card-theme-dark/);

      const labelVar = await algoViewport.evaluate((el) => {
        return getComputedStyle(el).getPropertyValue('--card-label-text').trim();
      });
      expect(
        labelVar.replace(/\s/g, ''),
        `Expected amber label text on ${route}`,
      ).toBe('#fbbf24');
    });
  }
});

test.describe('UX desktop: cream theme on public /performance', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test('PerformancePage wrapper has card-theme-cream and cream vars resolve', async ({ page }) => {
    await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded', timeout: 30000 });

    // The cream wrapper is the direct parent of PerformancePage's root element.
    const wrapper = page.locator('div.card-theme-cream').first();
    await expect(wrapper).toBeVisible({ timeout: 15000 });

    // 1. SSOT — --card-bg resolves to the cream value #f0ead8.
    const cardBgVar = await wrapper.evaluate((el) => {
      return getComputedStyle(el).getPropertyValue('--card-bg').trim();
    });
    expect(
      cardBgVar.includes('f0ead8'),
      `Expected cream bg for --card-bg on /performance, got: "${cardBgVar}"`
    ).toBe(true);

    // --card-gain-text must resolve to the deep green (#1a6b3a) for the
    // cream palette (as opposed to the dark palette's #4ade80).
    const gainVar = await wrapper.evaluate((el) => {
      return getComputedStyle(el).getPropertyValue('--card-gain-text').trim();
    });
    expect(gainVar.replace(/\s/g, '')).toBe('#1a6b3a');
  });
});

test.describe('UX mobile: cream theme on public /performance', () => {
  test.use({ viewport: { width: 393, height: 851 } });

  test('PerformancePage wrapper has cream vars on mobile', async ({ page }) => {
    await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded', timeout: 30000 });

    const wrapper = page.locator('div.card-theme-cream').first();
    await expect(wrapper).toBeVisible({ timeout: 15000 });

    const cardBgVar = await wrapper.evaluate((el) => {
      return getComputedStyle(el).getPropertyValue('--card-bg').trim();
    });
    expect(
      cardBgVar.includes('f0ead8'),
      `Expected cream bg on mobile /performance, got: "${cardBgVar}"`
    ).toBe(true);
  });
});

/* ── 5. Cross-page: algo dark → public cream → different algo dark ────── */
test.describe('Cross-page: theme vars switch correctly across navigation', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test('dark on /dashboard → cream on /performance → dark on /pulse (no bleed)', async ({ page }) => {
    await loginAsAdmin(page);

    // Step 1 — land on dashboard, verify dark via algo-viewport.
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    const algoViewport1 = page.locator('div.algo-viewport');
    await expect(algoViewport1).toBeVisible({ timeout: 15000 });
    const dashLabelVar = await algoViewport1.evaluate((el) =>
      getComputedStyle(el).getPropertyValue('--card-label-text').trim()
    );
    expect(dashLabelVar.replace(/\s/g, '')).toBe('#fbbf24');

    // Step 2 — navigate to public /performance, verify cream.
    await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    const perfWrapper = page.locator('div.card-theme-cream').first();
    await expect(perfWrapper).toBeVisible({ timeout: 15000 });
    const perfBgVar = await perfWrapper.evaluate((el) =>
      getComputedStyle(el).getPropertyValue('--card-bg').trim()
    );
    expect(
      perfBgVar.includes('f0ead8'),
      `Expected cream on /performance after nav from /dashboard, got: "${perfBgVar}"`
    ).toBe(true);

    // Step 3 — navigate to /pulse (different algo route), verify dark (no cream bleed).
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    const algoViewport2 = page.locator('div.algo-viewport');
    await expect(algoViewport2).toBeVisible({ timeout: 15000 });
    const pulseLabelVar = await algoViewport2.evaluate((el) =>
      getComputedStyle(el).getPropertyValue('--card-label-text').trim()
    );
    expect(pulseLabelVar.replace(/\s/g, '')).toBe('#fbbf24');
  });
});

/* ── 2. Perf: no extra XHR calls introduced ─────────────────────────── */
test.describe('Perf: no extra XHR calls from theme change', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test('/performance page XHR budget unchanged (≤35 API calls on cold load)', async ({ page }) => {
    let apiCallCount = 0;
    page.on('request', (req) => {
      if (req.url().includes('/api/') && req.resourceType() === 'fetch') {
        apiCallCount++;
      }
    });

    await page.goto(`${BASE}/performance`, { waitUntil: 'networkidle', timeout: 30000 });
    expect(apiCallCount).toBeLessThanOrEqual(35);
  });
});
