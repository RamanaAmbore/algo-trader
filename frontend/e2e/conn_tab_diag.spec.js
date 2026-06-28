/**
 * Diagnostic spec: "Conn tab does not have rows displayed"
 *
 * Checks three surfaces where the Conn tab appears:
 *   1. /activity page
 *   2. /orders Activity card
 *   3. ActivityLogModal (via navbar broker chip)
 *
 * For each surface captures:
 *   a. Screenshot
 *   b. .log-panel.log-rows .log-row count
 *   c. .log-panel.log-rows .log-debug count (empty-state sentinel)
 *   d. Inner text of first .log-row (truncated 200 chars)
 *   e. Network: did GET /api/admin/logs/conn fire? Status + size?
 *   f. Console errors/warnings
 *   g. document.querySelector('select.act-level-sel')?.value
 *   h. Did _t=Date.now() cache-buster appear in the request URL?
 *
 * Target: dev.ramboq.com ONLY (never prod).
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *     npx playwright test e2e/conn_tab_diag.spec.js --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

// Ensure artifacts directory exists (spec-level, not relying on shell).
const ARTIFACTS_DIR = path.join(process.cwd(), 'artifacts');
if (!fs.existsSync(ARTIFACTS_DIR)) fs.mkdirSync(ARTIFACTS_DIR, { recursive: true });

/**
 * Capture all diagnostic fields for the Conn tab in the currently-visible surface.
 * Assumes the Conn tab is already active (aria-selected="true" or just clicked).
 * @param {import('@playwright/test').Page} page
 * @param {string} surfaceName - label used in console output
 * @param {string} screenshotPath - absolute path for the screenshot
 * @param {import('@playwright/test').Response[]} connResponses - network responses accumulated by caller
 */
async function captureDiagnostics(page, surfaceName, screenshotPath, connResponses) {
  // Wait up to 6s for either rows to appear OR an empty-state sentinel.
  // Avoids hard-coding a fixed wait; resolves as soon as DOM settles.
  await page.waitForFunction(() => {
    const rows   = document.querySelectorAll('.log-panel.log-rows .log-row');
    const debug  = document.querySelectorAll('.log-panel.log-rows .log-debug');
    return rows.length > 0 || debug.length > 0;
  }, { timeout: 6000 }).catch(() => { /* capture whatever is there */ });

  // Screenshot
  await page.screenshot({ path: screenshotPath, fullPage: false });

  // DOM counts
  const rowCount   = await page.evaluate(() =>
    document.querySelectorAll('.log-panel.log-rows .log-row').length);
  const debugCount = await page.evaluate(() =>
    document.querySelectorAll('.log-panel.log-rows .log-debug').length);

  // First row text
  const firstRowText = await page.evaluate(() => {
    const el = document.querySelector('.log-panel.log-rows .log-row');
    return el ? (el.textContent || el.innerHTML || '').trim().slice(0, 200) : '(none)';
  });

  // Level filter value
  const levelFilterValue = await page.evaluate(() =>
    document.querySelector('select.act-level-sel')?.value ?? '(not found)');

  // Network: latest conn response (set by caller before clicking tab)
  const latestConn = connResponses.length > 0 ? connResponses[connResponses.length - 1] : null;
  let connStatus   = '(no request fired)';
  let connSize     = '(n/a)';
  let connUrlFull  = '(n/a)';
  let hasCacheBuster = false;
  if (latestConn) {
    connStatus   = String(latestConn.status());
    connUrlFull  = latestConn.url();
    hasCacheBuster = /[?&]_t=\d+/.test(connUrlFull);
    try {
      const body = await latestConn.body();
      connSize = `${body.length} bytes`;
    } catch (_) {
      connSize = '(body unavailable)';
    }
  }

  console.log(`
========== ${surfaceName} ==========
Screenshot      : ${screenshotPath}
.log-row count  : ${rowCount}
.log-debug count: ${debugCount}
First row text  : ${firstRowText}
Level filter    : ${levelFilterValue}
/api/admin/logs/conn request fired? : ${latestConn ? 'YES' : 'NO'}
  Response status : ${connStatus}
  Response size   : ${connSize}
  URL (truncated) : ${connUrlFull.slice(0, 200)}
  Cache-buster (_t=...) present? : ${hasCacheBuster}
`);

  return {
    surfaceName, rowCount, debugCount, firstRowText,
    levelFilterValue, connFired: !!latestConn,
    connStatus, connSize, connUrlFull, hasCacheBuster,
  };
}

test.describe('Conn tab diagnostic — three surfaces', () => {
  test.describe.configure({ mode: 'serial' });
  test.use({ viewport: { width: 1400, height: 900 } });
  test.setTimeout(90_000);

  // ── Surface 1: /activity page ─────────────────────────────────────────
  test('surface 1 — /activity page Conn tab', async ({ page }) => {
    const consoleMessages = [];
    page.on('console', msg => {
      if (msg.type() === 'error' || msg.type() === 'warning') {
        consoleMessages.push(`[${msg.type().toUpperCase()}] ${msg.text()}`);
      }
    });
    page.on('pageerror', err => {
      consoleMessages.push(`[PAGEERROR] ${err.message}`);
    });

    await loginAsAdmin(page);
    await page.goto(`${BASE}/activity`, { waitUntil: 'domcontentloaded' });

    // Collect all /api/admin/logs/conn responses after navigation.
    const connResponses = [];
    page.on('response', r => {
      if (r.url().includes('/api/admin/logs/conn')) connResponses.push(r);
    });

    // Wait for network to settle (orders + agent initial polls).
    await page.waitForLoadState('networkidle').catch(() => null);

    // Click the Conn tab.
    const connTab = page.locator('[role="tab"]:has-text("Conn")').first();
    await expect(connTab, 'Conn tab visible on /activity').toBeVisible({ timeout: 15_000 });
    await connTab.click();

    // Wait for the lazy conn poller to fire (deferred on first activation).
    // Either a /api/admin/logs/conn response arrives OR 4s elapses.
    await Promise.race([
      page.waitForResponse(r => r.url().includes('/api/admin/logs/conn'), { timeout: 6000 }),
      new Promise(res => setTimeout(res, 4000)),
    ]).catch(() => null);

    const result = await captureDiagnostics(
      page,
      'Surface 1: /activity page',
      path.join(ARTIFACTS_DIR, 'conn-tab-activity-page.png'),
      connResponses,
    );

    if (consoleMessages.length > 0) {
      console.log('Console messages on /activity:\n' + consoleMessages.join('\n'));
    }

    // Non-fatal assertions — report, don't fail hard, so the other surfaces run.
    // The test passes whether rows are present or absent; the diagnostics are the product.
    expect(typeof result.rowCount).toBe('number');
    expect(typeof result.levelFilterValue).toBe('string');
  });

  // ── Surface 2: /orders Activity card ─────────────────────────────────
  test('surface 2 — /orders Activity card Conn tab', async ({ page }) => {
    const consoleMessages = [];
    page.on('console', msg => {
      if (msg.type() === 'error' || msg.type() === 'warning') {
        consoleMessages.push(`[${msg.type().toUpperCase()}] ${msg.text()}`);
      }
    });
    page.on('pageerror', err => {
      consoleMessages.push(`[PAGEERROR] ${err.message}`);
    });

    await loginAsAdmin(page);
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });

    const connResponses = [];
    page.on('response', r => {
      if (r.url().includes('/api/admin/logs/conn')) connResponses.push(r);
    });

    await page.waitForLoadState('networkidle').catch(() => null);

    // The Activity card on /orders.
    const activityCard = page.locator('section.bucket-card-activity');
    await expect(activityCard, 'Activity card visible on /orders').toBeVisible({ timeout: 20_000 });
    await activityCard.scrollIntoViewIfNeeded();

    // Click Conn tab inside the activity card.
    const connTab = activityCard.locator('[role="tab"]:has-text("Conn")').first();
    await expect(connTab, 'Conn tab visible in Activity card').toBeVisible({ timeout: 15_000 });
    await connTab.click();

    await Promise.race([
      page.waitForResponse(r => r.url().includes('/api/admin/logs/conn'), { timeout: 6000 }),
      new Promise(res => setTimeout(res, 4000)),
    ]).catch(() => null);

    const result = await captureDiagnostics(
      page,
      'Surface 2: /orders Activity card',
      path.join(ARTIFACTS_DIR, 'conn-tab-orders-card.png'),
      connResponses,
    );

    if (consoleMessages.length > 0) {
      console.log('Console messages on /orders:\n' + consoleMessages.join('\n'));
    }

    expect(typeof result.rowCount).toBe('number');
    expect(typeof result.levelFilterValue).toBe('string');
  });

  // ── Surface 3: ActivityLogModal (navbar broker chip) ──────────────────
  test('surface 3 — ActivityLogModal Conn tab (via navbar broker chip)', async ({ page }) => {
    const consoleMessages = [];
    page.on('console', msg => {
      if (msg.type() === 'error' || msg.type() === 'warning') {
        consoleMessages.push(`[${msg.type().toUpperCase()}] ${msg.text()}`);
      }
    });
    page.on('pageerror', err => {
      consoleMessages.push(`[PAGEERROR] ${err.message}`);
    });

    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    const connResponses = [];
    page.on('response', r => {
      if (r.url().includes('/api/admin/logs/conn')) connResponses.push(r);
    });

    await page.waitForLoadState('networkidle').catch(() => null);

    // Open modal via broker chip.
    const chip = page.locator('button.broker-chip').first();
    await expect(chip, 'broker chip visible').toBeVisible({ timeout: 25_000 });
    await chip.click({ force: true });

    const overlay = page.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(overlay, 'ActivityLogModal overlay opens').toBeVisible({ timeout: 10_000 });

    // Modal opens on the Conn tab when chip is clicked (openActivityModal('conn')).
    // Find and click Conn tab if not already active.
    const connTab = overlay.locator('[role="tab"]:has-text("Conn")').first();
    await expect(connTab, 'Conn tab visible in modal').toBeVisible({ timeout: 10_000 });

    const isSelected = await connTab.getAttribute('aria-selected');
    if (isSelected !== 'true') {
      await connTab.click();
    }

    await Promise.race([
      page.waitForResponse(r => r.url().includes('/api/admin/logs/conn'), { timeout: 6000 }),
      new Promise(res => setTimeout(res, 4000)),
    ]).catch(() => null);

    // Scope to the overlay for the captureDiagnostics screenshot + DOM eval
    // (the overlay is always the top-most surface; DOM queries still work page-wide).
    const result = await captureDiagnostics(
      page,
      'Surface 3: ActivityLogModal (broker chip)',
      path.join(ARTIFACTS_DIR, 'conn-tab-modal.png'),
      connResponses,
    );

    if (consoleMessages.length > 0) {
      console.log('Console messages on /pulse (modal):\n' + consoleMessages.join('\n'));
    }

    expect(typeof result.rowCount).toBe('number');
    expect(typeof result.levelFilterValue).toBe('string');
  });
});
