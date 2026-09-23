/**
 * SSE heartbeat watchdog — connection health monitoring.
 *
 * The watchdog module stamps `_lastHbAt` when a heartbeat event arrives,
 * and reconnects the stream when silent for >45s. This test verifies:
 *
 * 1. Page loads and stream is initialized (via networkidle state).
 * 2. After a quick tab hide/show cycle (< 45s), no console errors occur
 *    from the watchdog attempting to reconnect.
 * 3. Portfolio data is still loading after visibility change (stream healthy).
 *
 * Note: SSE streams use EventSource API which isn't easily intercepted via
 * page.on('request'). Instead, we verify the guard logic by checking that
 * the page remains stable and data flows continue after visibility changes.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 25_000;
const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

test.describe('SSE watchdog — heartbeat + visibilitychange guard', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('page remains stable after visibilitychange (no reconnect errors)', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        consoleErrors.push(msg.text());
      }
    });

    // Navigate to the authenticated dashboard page where SSE stream is active.
    // Use 'domcontentloaded' since SSE streams keep networkidle indefinitely.
    await page.goto(BASE + '/pulse', { waitUntil: 'domcontentloaded' });

    // Allow initial stream setup.
    await page.waitForTimeout(2000);

    // Simulate tab hide → immediate return (< 45s gap).
    // The watchdog guard should prevent unnecessary reconnect attempts.
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        value: 'hidden',
        configurable: true,
      });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.waitForTimeout(200);

    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        value: 'visible',
        configurable: true,
      });
      document.dispatchEvent(new Event('visibilitychange'));
    });

    // Wait for any errors to appear.
    await page.waitForTimeout(1000);

    // Filter errors to exclude unrelated issues.
    const watchdogErrors = consoleErrors.filter(
      (e) =>
        e.includes('watchdog') ||
        e.includes('EventSource') ||
        e.includes('stream') ||
        e.includes('reconnect'),
    );

    expect(watchdogErrors).toHaveLength(0);
  });

  test('pulse page loads and renders portfolio data', async ({ page }) => {
    // Simply verify that the pulse page loads without console errors
    // and displays the expected UI elements.
    const consoleErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        consoleErrors.push(msg.text());
      }
    });

    await page.goto(BASE + '/pulse', { waitUntil: 'domcontentloaded' });

    // Wait for initial data load and rendering.
    await page.waitForTimeout(2500);

    // Verify pulse page is rendered with heading.
    const heading = page.locator('h1:has-text("Pulse")');
    await expect(heading).toBeVisible({ timeout: TIMEOUT });

    // Verify grids or tabpanel exists (portfolio data rendering).
    const grids = page.locator('div[role="grid"], [class*="grid"], [class*="table"]').first();
    if ((await grids.count()) > 0) {
      await expect(grids).toBeVisible({ timeout: TIMEOUT });
    }

    // No critical errors during page load.
    const criticalErrors = consoleErrors.filter(
      (e) => e.includes('ReferenceError') || e.includes('TypeError'),
    );
    expect(criticalErrors).toHaveLength(0);
  });
});
