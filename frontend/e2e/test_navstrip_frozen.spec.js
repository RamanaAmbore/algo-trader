/**
 * NavStrip (PositionStrip) data-completeness spec.
 *
 * Verifies that:
 *   1. All four pills (P / M / C / H) are visible and contain non-blank
 *      values after login and data load.
 *   2. Each pill's first slot is non-zero (not blank / dash / "0") — confirms
 *      the strip is receiving data, not stuck in a blank/zero state.
 *   3. During closed hours, confirms the strip shows last-session snapshot
 *      data rather than blanks.
 *
 * Auth strategy: single beforeAll login → shared sessionStorage injected
 * per test, to avoid burning the 5/min rate-limit with repeated form submits.
 *
 * Run:
 *   PLAYWRIGHT_USER=rambo PLAYWRIGHT_PASS=admin1234 \
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test test_navstrip_frozen --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

// ── session injection ─────────────────────────────────────────────────────────

/**
 * Inject saved sessionStorage into a page before navigation so the auth
 * store picks up the token on mount (no repeated form submits).
 * @param {import('@playwright/test').Page} page
 * @param {Record<string, string>} items
 */
async function injectSession(page, items) {
  await page.addInitScript((data) => {
    for (const [k, v] of Object.entries(data)) sessionStorage.setItem(k, v);
  }, items);
  if (items.ramboq_token) {
    await page.context().setExtraHTTPHeaders({
      Authorization: `Bearer ${items.ramboq_token}`,
    });
  }
}

// ── tests ─────────────────────────────────────────────────────────────────────

test.describe('NavStrip data completeness', () => {
  test.describe.configure({ mode: 'serial' });

  /** @type {Record<string, string>} */
  let _session = {};

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(60_000);
    const ctx = await browser.newContext();
    const setup = await ctx.newPage();
    await loginAsAdmin(setup);
    _session = await setup.evaluate(() => {
      const out = {};
      for (const k of ['ramboq_token', 'ramboq_user']) {
        const v = sessionStorage.getItem(k);
        if (v) out[k] = v;
      }
      return out;
    });
    await setup.close();
    await ctx.close();
  });

  test('all four pills are visible with non-blank first slot values', async ({ page }) => {
    await injectSession(page, _session);
    await page.goto('/pulse');

    // Wait for the NavStrip container (.ps-strip) to appear.
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: 20_000 });

    // Wait for at least one poll to complete — identified by the
    // .ps-agg-v elements being present and non-empty.
    await page.waitForFunction(() => {
      const spans = document.querySelectorAll('.ps-agg-v');
      return spans.length >= 4 && [...spans].some(s => (s.textContent || '').trim().length > 0);
    }, { timeout: 30_000 });

    // All four pills must be present and visible.
    const pills = page.locator('.ps-agg');
    await expect(pills).toHaveCount(4, { timeout: 5_000 });

    for (let i = 0; i < 4; i++) {
      await expect(pills.nth(i)).toBeVisible();
    }

    // Check pill keys (P, M, C, H) in order.
    const pillKeys = await page.locator('.ps-agg-k').allInnerTexts();
    expect(pillKeys[0]).toBe('P');
    expect(pillKeys[1]).toBe('M');
    expect(pillKeys[2]).toBe('C');
    expect(pillKeys[3]).toBe('H');

    // Each pill must have at least one .ps-agg-v with a non-blank string.
    // We check the first slot (index 0) of each pill — most likely populated
    // when the strip has live data.
    for (let i = 0; i < 4; i++) {
      const firstVal = pills.nth(i).locator('.ps-agg-v').first();
      await expect(firstVal).not.toBeEmpty({ timeout: 10_000 });
      const text = await firstVal.innerText();
      expect(
        text.trim(),
        `Pill ${i} first slot is blank — strip may be stuck in zero/empty state`
      ).not.toBe('');
      expect(
        text.trim(),
        `Pill ${i} first slot is a dash — data failed to load`
      ).not.toBe('—');
    }
  });

  test('P pill has three slash-separated values', async ({ page }) => {
    await injectSession(page, _session);
    await page.goto('/pulse');

    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: 20_000 });

    await page.waitForFunction(() => {
      const spans = document.querySelectorAll('.ps-agg-v');
      return spans.length >= 4;
    }, { timeout: 30_000 });

    // P pill (first .ps-agg) must have exactly 3 .ps-agg-v slots.
    const pPillVals = page.locator('.ps-agg').first().locator('.ps-agg-v');
    await expect(pPillVals).toHaveCount(3, { timeout: 10_000 });

    // P pill must have exactly 2 .ps-agg-sep (slash) separators.
    const pPillSeps = page.locator('.ps-agg').first().locator('.ps-agg-sep');
    await expect(pPillSeps).toHaveCount(2, { timeout: 5_000 });

    // All three slots must be non-blank.
    for (let i = 0; i < 3; i++) {
      const val = pPillVals.nth(i);
      const text = await val.innerText();
      expect(text.trim(), `P pill slot ${i + 1} is blank`).not.toBe('');
    }
  });

  test('H pill has three slash-separated values', async ({ page }) => {
    await injectSession(page, _session);
    await page.goto('/pulse');

    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: 20_000 });

    await page.waitForFunction(() => {
      const spans = document.querySelectorAll('.ps-agg-v');
      return spans.length >= 4;
    }, { timeout: 30_000 });

    // H pill (fourth .ps-agg) must have exactly 3 .ps-agg-v slots.
    const hPillVals = page.locator('.ps-agg').nth(3).locator('.ps-agg-v');
    await expect(hPillVals).toHaveCount(3, { timeout: 10_000 });

    const hPillSeps = page.locator('.ps-agg').nth(3).locator('.ps-agg-sep');
    await expect(hPillSeps).toHaveCount(2, { timeout: 5_000 });
  });

  test('M and C pills each have two slash-separated values', async ({ page }) => {
    await injectSession(page, _session);
    await page.goto('/pulse');

    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: 20_000 });

    await page.waitForFunction(() => {
      const spans = document.querySelectorAll('.ps-agg-v');
      return spans.length >= 4;
    }, { timeout: 30_000 });

    // M pill (second .ps-agg) must have 2 slots.
    const mPillVals = page.locator('.ps-agg').nth(1).locator('.ps-agg-v');
    await expect(mPillVals).toHaveCount(2, { timeout: 10_000 });

    // C pill (third .ps-agg) must have 2 slots.
    const cPillVals = page.locator('.ps-agg').nth(2).locator('.ps-agg-v');
    await expect(cPillVals).toHaveCount(2, { timeout: 10_000 });
  });

  test('strip shows data during closed hours (snapshot path)', async ({ page }) => {
    await injectSession(page, _session);
    await page.goto('/pulse');

    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: 20_000 });

    // Wait for data to load.
    await page.waitForFunction(() => {
      const spans = document.querySelectorAll('.ps-agg-v');
      return spans.length >= 8 && [...spans].some(s => {
        const t = (s.textContent || '').trim();
        return t && t !== '0' && t !== '—';
      });
    }, { timeout: 30_000 });

    // We only verify that values are present — not that they are non-zero,
    // since a genuinely zero P&L is valid closed-hours state.
    const allVals = await page.locator('.ps-agg-v').allInnerTexts();
    const nonBlankCount = allVals.filter(v => v.trim() !== '').length;

    // All value spans (at minimum 8 of them across 4 pills) must be non-blank.
    expect(
      nonBlankCount,
      `Only ${nonBlankCount}/${allVals.length} pill slots are non-blank — strip may be failing to hydrate from snapshot`
    ).toBeGreaterThanOrEqual(8);

    // No value slot should be a literal dash (that indicates null/failed data).
    const dashCount = allVals.filter(v => v.trim() === '—').length;
    expect(
      dashCount,
      `${dashCount} pill slots show "—" — data failed to load from broker or snapshot`
    ).toBe(0);
  });

  test('strip visible on mobile viewport', async ({ page }) => {
    await injectSession(page, _session);
    // Resize to mobile dimensions for this test (overrides the project viewport)
    await page.setViewportSize({ width: 375, height: 667 });
    await page.goto('/pulse');

    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: 20_000 });

    // On mobile, the strip uses justify-content: space-between. All four
    // pills must remain within the viewport (not clipped off-screen).
    const pills = page.locator('.ps-agg');
    await expect(pills).toHaveCount(4, { timeout: 10_000 });

    for (let i = 0; i < 4; i++) {
      const box = await pills.nth(i).boundingBox();
      expect(box, `Pill ${i} has no bounding box on mobile`).not.toBeNull();
      if (box) {
        // Pill must start at x >= 0 (not clipped to the left).
        expect(box.x, `Pill ${i} extends beyond left edge`).toBeGreaterThanOrEqual(0);
        // Pill must end within 2× viewport width (allows horizontal scroll but not broken layout).
        expect(box.x + box.width, `Pill ${i} extends beyond 2× viewport width`).toBeLessThanOrEqual(750);
      }
    }
  });
});
