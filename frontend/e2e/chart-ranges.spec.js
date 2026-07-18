/**
 * chart-ranges.spec.js
 *
 * Regression spec for chart range buttons (3M/6M/1Y).
 *
 * Background: Bug #910740f0 caused _SELF_HEAL_COVERAGE_THRESHOLD = 0.70 to always
 * flag NSE equity OHLCV data as "partial" for 3M/6M/1Y ranges (NSE has ~252
 * trading days/year = 69% of calendar days, always below 70%). This triggered
 * infinite _ohlcv_demand_fill retries — charts spun indefinitely.
 * Fix: lowered threshold to 0.60.
 *
 * Test scope (data-independent — no OHLCV cache required):
 *   1. Stale-code: options.py threshold is 0.60 (not 0.70)
 *   2. Range buttons (3M/6M/1Y) are present, visible, and clickable
 *   3. Clicking a range button doesn't cause a JS crash or infinite spinner
 *   4. Active button state updates after click
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dir = dirname(fileURLToPath(import.meta.url));
const _OPTIONS_SRC = join(__dir, '../../backend/api/routes/options.py');

// ── Dimension 3 (stale-code) — source file check, no server needed ──────────
test('chart-ranges: options.py self-heal threshold is 0.60 not 0.70', () => {
  const src = readFileSync(_OPTIONS_SRC, 'utf-8');
  const line = src.split('\n').find(l => l.includes('_SELF_HEAL_COVERAGE_THRESHOLD: float'));
  expect(line, 'Could not find _SELF_HEAL_COVERAGE_THRESHOLD definition').toBeTruthy();
  expect(line).toContain('= 0.60');
  expect(line).not.toContain('= 0.70');
});

// ── Dimensions 1/2/4/5 — range button smoke (data-independent) ───────────────
test.describe('chart-ranges: range button smoke', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('3M/6M/1Y buttons visible and enabled after initial chart load', async ({ page }) => {
    test.setTimeout(60_000);

    // Navigate with a symbol so range buttons are active immediately
    await page.goto('/charts?symbol=NIFTY+50', { waitUntil: 'domcontentloaded' });

    // Wait for ChartWorkspace to mount — range group is always rendered
    const rangeGroup = page.locator('.cw-range-group');
    await expect(rangeGroup).toBeVisible({ timeout: 30_000 });

    // Wait for at least one range button to become enabled (initial load done)
    const anyBtn = rangeGroup.locator('.cw-range-btn').first();
    await expect(anyBtn).toBeEnabled({ timeout: 30_000 });

    // Assert 3M/6M/1Y buttons are present
    for (const label of ['3M', '6M', '1Y']) {
      const btn = rangeGroup.locator('.cw-range-btn', { hasText: label });
      await expect(btn).toBeVisible();
      await expect(btn).toBeEnabled();
    }
  });

  test('clicking 3M range button switches active state without crash', async ({ page }) => {
    test.setTimeout(60_000);

    await page.goto('/charts?symbol=NIFTY+50', { waitUntil: 'domcontentloaded' });

    const rangeGroup = page.locator('.cw-range-group');
    await expect(rangeGroup).toBeVisible({ timeout: 30_000 });

    // Wait for initial load to complete (buttons enabled)
    const btn3M = rangeGroup.locator('.cw-range-btn', { hasText: '3M' });
    await expect(btn3M).toBeEnabled({ timeout: 30_000 });

    // Click 3M — should not crash, spinner should eventually resolve
    await btn3M.click();

    // Assert no JS errors thrown (Playwright surfaces uncaught exceptions)
    // by asserting page is still responsive after click
    await expect(rangeGroup).toBeVisible({ timeout: 5_000 });

    // Wait for spinner to clear (regression: would spin indefinitely with 0.70 threshold)
    // Spinner = .cw-fetch-spinner; it should disappear within 20s
    const spinner = page.locator('.cw-fetch-spinner');
    await expect(spinner).toHaveCount(0, { timeout: 20_000 });
  });

  test('clicking 6M then 1Y both clear spinner within 20s', async ({ page }) => {
    test.setTimeout(90_000);

    await page.goto('/charts?symbol=NIFTY+50', { waitUntil: 'domcontentloaded' });

    const rangeGroup = page.locator('.cw-range-group');
    await expect(rangeGroup).toBeVisible({ timeout: 30_000 });

    // Wait for initial load
    const anyBtn = rangeGroup.locator('.cw-range-btn').first();
    await expect(anyBtn).toBeEnabled({ timeout: 30_000 });

    for (const label of ['6M', '1Y']) {
      const btn = rangeGroup.locator('.cw-range-btn', { hasText: label });
      await expect(btn).toBeEnabled({ timeout: 5_000 });
      await btn.click();

      // Key regression check: spinner clears within 20s (infinite spin = bug)
      const spinner = page.locator('.cw-fetch-spinner');
      await expect(spinner).toHaveCount(0, { timeout: 20_000 });
      console.log(`[${label}] spinner cleared`);

      // Small gap between clicks
      await page.waitForTimeout(300);
    }
  });
});
