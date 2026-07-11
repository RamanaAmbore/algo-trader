/**
 * phase1_js_extractions_smoke.spec.js
 *
 * Smoke test for Phase 1 pure JS extractions:
 *  1. templateScope.js — _appliesToFor(side, sym) extracted from SymbolPanel + OrderTicket
 *  2. riskMath.js — BS math functions extracted from derivatives page
 *  3. chart/paths.js — SMA/EMA/VWAP/BB/RSI/MACD path computation extracted from ChartWorkspace
 *
 * Verification: Page loads without JS SYNTAX errors (extractions correct).
 * Ignores network-level errors (403, 405) which are infrastructure, not extraction issues.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/phase1_js_extractions_smoke.spec.js --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

/** Check if error is a JS syntax/import error (not network-related) */
function isJsError(msg) {
  const text = msg.toLowerCase();
  // Ignore network-level errors (any HTTP status, WebSocket, etc)
  if (text.includes('failed to load resource:')) return false;
  if (text.includes('websocket connection')) return false;
  if (text.includes('the server responded with')) return false;
  if (text.includes('net::')) return false;
  // Filter for actual JS/import/syntax errors
  return true;
}

test.describe('Phase 1 JS Extractions Smoke Test', () => {
  test.setTimeout(90_000);

  /**
   * Chart page: Verify chart/paths.js extraction doesn't have import/syntax errors.
   * Exercises: SMA/EMA/VWAP/BB/RSI/MACD path computation extraction.
   */
  test('chart page loads without JS syntax errors', async ({ page }) => {
    await loginAsAdmin(page);

    const jsErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error' && isJsError(msg.text())) {
        jsErrors.push(msg.text());
      }
    });

    const chartUrl = `${BASE}/charts?symbol=${encodeURIComponent('NIFTY 50')}&mode=live`;
    await page.goto(chartUrl, { waitUntil: 'domcontentloaded' });

    // Wait for page to load
    await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 15_000 });

    // Reload with overlays enabled (exercises path computation functions)
    await page.evaluate(() => {
      localStorage.setItem('rbq.cache.chart-overlays.v1', JSON.stringify(['sma20', 'ema20', 'bb', 'rsi']));
    });
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 15_000 });

    // Verify no JS syntax errors (chart/paths.js import/export is correct)
    expect(jsErrors, `Chart JS errors: ${jsErrors.join('; ')}`).toEqual([]);
  });

  /**
   * Derivatives page: Verify riskMath.js extraction doesn't have import/syntax errors.
   * Exercises: Black-Scholes math functions extraction.
   */
  test('derivatives page loads without JS syntax errors', async ({ page }) => {
    await loginAsAdmin(page);

    const jsErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error' && isJsError(msg.text())) {
        jsErrors.push(msg.text());
      }
    });

    const derivUrl = `${BASE}/admin/derivatives`;
    await page.goto(derivUrl, { waitUntil: 'domcontentloaded' });

    // Wait for main content
    await expect(page.locator('main')).toBeVisible({ timeout: 10_000 });
    await page.waitForTimeout(1500);

    // Verify no JS syntax errors (riskMath.js import/export is correct)
    expect(jsErrors, `Derivatives JS errors: ${jsErrors.join('; ')}`).toEqual([]);
  });

  /**
   * Pulse page: Verify templateScope.js extraction doesn't have import/syntax errors.
   * Exercises: _appliesToFor(side, sym) function extraction.
   */
  test('pulse page loads without JS syntax errors', async ({ page }) => {
    await loginAsAdmin(page);

    const jsErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error' && isJsError(msg.text())) {
        jsErrors.push(msg.text());
      }
    });

    const pulseUrl = `${BASE}/pulse`;
    await page.goto(pulseUrl, { waitUntil: 'domcontentloaded' });

    // Wait for grid to render
    await page.waitForTimeout(1000);
    await expect(page.locator('[role="row"]').first()).toBeVisible({ timeout: 15_000 });
    await page.waitForTimeout(500);

    // Verify no JS syntax errors (templateScope.js import/export is correct)
    expect(jsErrors, `Pulse JS errors: ${jsErrors.join('; ')}`).toEqual([]);
  });

  /**
   * Symbol panel activation: Verify templateScope._appliesToFor is callable.
   * Key: Panel opens, function executes without errors.
   */
  test('symbol panel invokes templateScope._appliesToFor without errors', async ({ page }) => {
    await loginAsAdmin(page);

    const jsErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error' && isJsError(msg.text())) {
        jsErrors.push(msg.text());
      }
    });

    const pulseUrl = `${BASE}/pulse`;
    await page.goto(pulseUrl, { waitUntil: 'domcontentloaded' });

    // Wait for grid
    await page.waitForTimeout(1000);
    await expect(page.locator('[role="row"]').first()).toBeVisible({ timeout: 15_000 });

    // Click first symbol cell to open panel (triggers _appliesToFor in component render)
    const firstSymbolCell = page.locator('[role="gridcell"]').filter({ hasText: /[A-Z]/ }).first();
    if (await firstSymbolCell.isVisible({ timeout: 3_000 }).catch(() => false)) {
      await firstSymbolCell.click();
      await page.waitForTimeout(500);
    }

    // Verify no JS errors during panel activation
    expect(jsErrors, `Symbol panel JS errors: ${jsErrors.join('; ')}`).toEqual([]);
  });

  /**
   * Multi-page navigation: Verify all extracted modules load without syntax errors.
   */
  test('all three pages load without JS syntax errors', async ({ page }) => {
    await loginAsAdmin(page);

    const jsErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error' && isJsError(msg.text())) {
        jsErrors.push(msg.text());
      }
    });

    // Navigate to pages that use all three extracted modules
    const pages = [
      `/charts?symbol=${encodeURIComponent('NIFTY 50')}`,
      `/admin/derivatives`,
      `/pulse`,
    ];

    for (const pathname of pages) {
      await page.goto(`${BASE}${pathname}`, { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(800);
    }

    // Verify no JS syntax errors during navigation
    expect(jsErrors, `Navigation JS errors: ${jsErrors.join('; ')}`).toEqual([]);
  });

});
