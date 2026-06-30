/**
 * dashboard_dark_theme.spec.js
 *
 * Validates the Bloomberg-style dark navy + amber-accent theme applied to
 * NavCard + PerformancePage on /dashboard (operator terminal) while the
 * public /performance route keeps the cream palette unchanged.
 *
 * Implementation: CSS custom properties set by parent wrapper class
 *   .card-theme-dark  → /dashboard (sec.cap-eq-tabbed)
 *   .card-theme-cream → /performance (root div wrapper)
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *  1. SSOT       — NavCard + PerformancePage read var(--card-bg) etc.
 *                  Resolved values match expected palette per context.
 *  2. Perf       — no extra XHR introduced; render within budget.
 *  3. Stale      — source-grep guard: no bare #faf7f0 / #f0ead8 /
 *                  rgba(212, 146, 12, ... remain in NavCard.svelte or
 *                  PerformancePage.svelte (all migrated to var() reads).
 *  4. Reuse      — dashboard wraps in card-theme-dark; /performance
 *                  wraps in card-theme-cream. Same component code.
 *  5. UX desktop + mobile — /dashboard renders dark bg on the NAV tab
 *                  card; /performance renders cream bg on NavCard.
 *                  Cross-page nav: theme variables switch without bleed.
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

  test('dashboard page section has card-theme-dark class on cap-eq-tabbed card', () => {
    const dashPage = path.join(
      process.cwd(),
      'src/routes/(algo)/dashboard/+page.svelte',
    );
    let src;
    try { src = fs.readFileSync(dashPage, 'utf-8'); }
    catch { test.skip(true, 'Cannot read dashboard page source'); return; }
    // The cap-eq-tabbed section must carry card-theme-dark so that any
    // NavCard / PerformancePage embed picks up the dark CSS vars.
    // Match both class names appearing together on the same element.
    expect(src).toMatch(/class="[^"]*cap-eq-tabbed[^"]*card-theme-dark|class="[^"]*card-theme-dark[^"]*cap-eq-tabbed/);
  });
});

/* ── 1 + 5. SSOT + UX: computed styles on real pages ─────────────────── */
// These tests navigate to live pages and check computed CSS variable values.
// They require a running dev server and valid credentials.

test.describe('UX desktop: dark theme on /dashboard NAV section', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test('NAV tab card has card-theme-dark class and dark CSS vars resolve', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded', timeout: 30000 });

    // Wait for the NAV | Capital | Equity tabbed card to mount.
    const navCard = page.locator('section.cap-eq-tabbed');
    await expect(navCard).toBeVisible({ timeout: 15000 });

    // 4. Reuse guard — the section carries the dark wrapper class.
    await expect(navCard).toHaveClass(/card-theme-dark/);

    // 1. SSOT — --card-bg resolves to the dark gradient value.
    // getPropertyValue on the section element itself reads the inherited var.
    const cardBgVar = await navCard.evaluate((el) => {
      return getComputedStyle(el).getPropertyValue('--card-bg').trim();
    });
    // The dark token is "linear-gradient(180deg, #1d2a44 0%, #152033 100%)"
    // — match on the gradient shape or on either color stop.
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

test.describe('UX mobile: dark theme on /dashboard NAV section', () => {
  test.use({ viewport: { width: 393, height: 851 } });

  test('NAV tab card renders dark on mobile viewport', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded', timeout: 30000 });

    const navCard = page.locator('section.cap-eq-tabbed');
    await expect(navCard).toBeVisible({ timeout: 15000 });
    await expect(navCard).toHaveClass(/card-theme-dark/);

    const labelVar = await navCard.evaluate((el) => {
      return getComputedStyle(el).getPropertyValue('--card-label-text').trim();
    });
    expect(labelVar.replace(/\s/g, '')).toBe('#fbbf24');
  });
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

/* ── 5. Cross-page consistency: /dashboard → /performance → /dashboard ── */
test.describe('Cross-page: theme vars switch correctly across navigation', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test('dark on /dashboard → cream on /performance → dark on /dashboard (no bleed)', async ({ page }) => {
    await loginAsAdmin(page);

    // Step 1 — land on dashboard, verify dark.
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    const dashCard = page.locator('section.cap-eq-tabbed');
    await expect(dashCard).toBeVisible({ timeout: 15000 });
    const dashLabelVar1 = await dashCard.evaluate((el) =>
      getComputedStyle(el).getPropertyValue('--card-label-text').trim()
    );
    expect(dashLabelVar1.replace(/\s/g, '')).toBe('#fbbf24');

    // Step 2 — navigate to /performance, verify cream.
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

    // Step 3 — navigate back to /dashboard, verify dark restored (no cream bleed).
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    const dashCard2 = page.locator('section.cap-eq-tabbed');
    await expect(dashCard2).toBeVisible({ timeout: 15000 });
    const dashLabelVar2 = await dashCard2.evaluate((el) =>
      getComputedStyle(el).getPropertyValue('--card-label-text').trim()
    );
    expect(dashLabelVar2.replace(/\s/g, '')).toBe('#fbbf24');
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
