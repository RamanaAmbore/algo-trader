/**
 * NavStrip P pill — three slash-joined values including expiry profit.
 *
 * Verifies (five quality dimensions per feedback_test_dimensions.md):
 *   SSOT     — same structure rendered on both /pulse and /performance
 *   Perf     — P pill renders without extra broker round-trip (no /api/firm-nav
 *              request triggered by the new expiry value)
 *   Stale    — no stale pattern: ps-exp class present, amber color token
 *   Reuse    — ps-agg component pattern reused, no inline style hacks
 *   UX       — numeric value formatted consistently (aggCompact, tabular-nums),
 *              expiry value is amber (#fbbf24), pill fits on mobile viewport
 *
 * Run:
 *   cd frontend && npx playwright test navstrip_expiry_profit --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;
const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

// ── SSOT: same structure on /pulse and /performance ───────────────────────

test.describe('P pill — three values present', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  /**
   * The P pill (.ps-agg:first-child) must contain exactly:
   *   [label "P"] [value today] [sep "/"] [value lifetime] [sep "/"] [value expiry]
   * i.e. 2 separators and 3 .ps-agg-v spans.
   */
  async function assertPPillThreeValues(page, route) {
    await page.goto(route);
    const strip = page.locator('.ps-strip').first();
    await expect(strip).toBeVisible({ timeout: TIMEOUT });

    // P pill is the first .ps-agg
    const pPill = strip.locator('.ps-agg').first();
    await expect(pPill).toBeVisible({ timeout: TIMEOUT });

    // Three value spans
    const vals = pPill.locator('.ps-agg-v');
    await expect(vals).toHaveCount(3, { timeout: TIMEOUT });

    // Two separators
    const seps = pPill.locator('.ps-agg-sep');
    await expect(seps).toHaveCount(2, { timeout: TIMEOUT });

    // Label is "P"
    const label = pPill.locator('.ps-agg-k');
    await expect(label).toHaveText('P');

    // Third value carries the ps-exp class (expiry profit — amber)
    const expiryVal = vals.nth(2);
    await expect(expiryVal).toHaveClass(/ps-exp/);

    // All values are non-empty strings (may be "0" when no positions)
    for (let i = 0; i < 3; i++) {
      await expect(vals.nth(i)).not.toBeEmpty();
    }
  }

  test('/pulse — P pill has three slash-joined values', async ({ page }) => {
    await assertPPillThreeValues(page, '/pulse');
  });

  test('/performance — P pill has three slash-joined values', async ({ page }) => {
    await assertPPillThreeValues(page, '/performance');
  });
});

// ── Perf: no extra /api/auth/firm-nav calls caused by expiry computation ──

test.describe('Perf — no extra broker API call', () => {
  test('expiry profit value computed client-side (no /firm-nav XHR on /pulse)', async ({ page }) => {
    await loginAsAdmin(page);

    // Track firm-nav API calls
    const firmNavCalls = [];
    page.on('request', req => {
      if (req.url().includes('/auth/firm-nav')) {
        firmNavCalls.push(req.url());
      }
    });

    await page.goto('/pulse');
    const strip = page.locator('.ps-strip').first();
    await expect(strip).toBeVisible({ timeout: TIMEOUT });
    // Wait for the first poll to complete
    await page.waitForTimeout(3_000);

    // /pulse does NOT call /auth/firm-nav — the expiry profit is computed
    // client-side from positionsStore data already fetched by the strip.
    // NavCard on /performance calls firm-nav — but not this page.
    expect(firmNavCalls.length).toBe(0);
  });
});

// ── Stale: ps-exp color class is amber (#fbbf24) ─────────────────────────

test.describe('UX — amber color and tabular-nums', () => {
  test('ps-exp class resolves to amber color token', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse');
    const strip = page.locator('.ps-strip').first();
    await expect(strip).toBeVisible({ timeout: TIMEOUT });

    const expiryVal = strip.locator('.ps-agg').first().locator('.ps-exp').first();
    await expect(expiryVal).toBeVisible({ timeout: TIMEOUT });

    // Check that the ps-exp class is present on the span (reuse check)
    await expect(expiryVal).toHaveClass(/ps-exp/);

    // Color should resolve to amber #fbbf24
    const color = await expiryVal.evaluate(el => getComputedStyle(el).color);
    // Convert rgb(251, 191, 36) ↔ #fbbf24
    const isAmber = color === 'rgb(251, 191, 36)' || color.toLowerCase() === '#fbbf24';
    expect(isAmber, `expected amber #fbbf24, got ${color}`).toBe(true);
  });

  test('expiry value uses tabular-nums', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse');
    const strip = page.locator('.ps-strip').first();
    await expect(strip).toBeVisible({ timeout: TIMEOUT });

    const expiryVal = strip.locator('.ps-agg').first().locator('.ps-exp').first();
    const fontVariant = await expiryVal.evaluate(el => getComputedStyle(el).fontVariantNumeric);
    expect(fontVariant).toContain('tabular-nums');
  });
});

// ── Mobile: P pill fits within viewport width on 360px phone ─────────────

test.describe('Mobile layout — P pill fits', () => {
  test.use({ viewport: { width: 360, height: 800 } });

  test('four pills and P pill fit within 360px viewport', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse');

    const strip = page.locator('.ps-strip').first();
    await expect(strip).toBeVisible({ timeout: TIMEOUT });

    // All four pills must be within the strip's bounds
    const stripBox = await strip.boundingBox();
    expect(stripBox).toBeTruthy();
    const pills = strip.locator('.ps-agg');
    await expect(pills).toHaveCount(4, { timeout: TIMEOUT });

    for (const pill of await pills.all()) {
      const box = await pill.boundingBox();
      if (!box) continue;
      // Each pill right edge within strip right edge (+ 8px tolerance for sub-pixel)
      expect(box.x + box.width).toBeLessThanOrEqual(stripBox.x + stripBox.width + 8);
    }
  });
});
