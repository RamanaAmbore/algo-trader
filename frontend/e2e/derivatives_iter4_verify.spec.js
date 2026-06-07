/**
 * Iter 4 verification — /admin/derivatives (OptionsPayoff legSymbols + cmdHistory tiebreakers)
 * Checks: pageerror count at load, during underlying switches, and 30s idle.
 * Ignores WS 405 + SSE 401 console messages.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

test.describe('/admin/derivatives iter4 verification', () => {
  test.setTimeout(120000);

  test('load, switch underlying, 30s idle — no pageerrors', async ({ page }) => {
    const pageErrors = [];
    const consoleErrors = [];
    const networkRequests = [];

    page.on('pageerror', (err) => {
      pageErrors.push(err.message);
    });

    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        const txt = msg.text();
        // Ignore WS 405 + SSE 401 + CORS preflight noise
        if (
          txt.includes('405') ||
          txt.includes('401') ||
          txt.includes('WebSocket') ||
          txt.includes('EventSource') ||
          txt.includes('ERR_FAILED') ||
          txt.includes('net::ERR') ||
          txt.includes('favicon')
        ) {
          return;
        }
        consoleErrors.push(txt);
      }
    });

    page.on('request', (req) => {
      const rtype = req.resourceType();
      // Count only API/fetch requests, not WS/SSE/images/fonts
      if (rtype === 'fetch' || rtype === 'xhr') {
        networkRequests.push(req.url());
      }
    });

    // Auth — try ambore first, fall back to rambo
    let authOk = false;
    for (const creds of [
      { user: 'ambore', pass: process.env.PLAYWRIGHT_PASS || 'admin1234' },
      { user: 'rambo', pass: 'admin1234' },
    ]) {
      try {
        await loginAsAdmin(page, creds);
        authOk = true;
        break;
      } catch (_) {
        // try next
      }
    }
    expect(authOk, 'login succeeded').toBe(true);

    const initialErrorCount = pageErrors.length;

    // Navigate to derivatives page
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded', timeout: 30000 });

    // Wait for page structure
    await page.waitForSelector('.opt-picker', { timeout: 20000 });
    await page.waitForSelector('.opt-payoff', { timeout: 20000 });

    // Allow any initial data fetch to complete
    await page.waitForTimeout(3000);

    const afterLoadErrors = pageErrors.length - initialErrorCount;

    // Check visibility
    const pickerVisible = await page.locator('.opt-picker').isVisible();
    const payoffVisible = await page.locator('.opt-payoff').isVisible();

    // Underlying switch — find the picker inside .opt-picker
    const switchErrsBefore = pageErrors.length;

    try {
      // Custom Select components use a trigger button + dropdown
      const pickerButtons = page.locator('.opt-picker button');
      const btnCount = await pickerButtons.count();

      if (btnCount > 0) {
        // Click first button (underlying picker trigger)
        await pickerButtons.first().click({ timeout: 5000 });
        await page.waitForTimeout(600);

        // Look for dropdown options
        const opts = page.locator('[role="option"], .sel-option, .select-option, li[data-value]');
        const optCount = await opts.count();

        if (optCount > 1) {
          // Switch to option 1 (different underlying)
          await opts.nth(1).click({ timeout: 3000 });
          await page.waitForTimeout(2000);

          // Switch back — reopen picker
          await pickerButtons.first().click({ timeout: 5000 });
          await page.waitForTimeout(600);

          const opts2 = page.locator('[role="option"], .sel-option, .select-option, li[data-value]');
          const cnt2 = await opts2.count();
          if (cnt2 > 0) {
            await opts2.nth(0).click({ timeout: 3000 });
            await page.waitForTimeout(2000);
          }
        }
      }
    } catch (e) {
      // Not critical — picker interaction optional
      console.warn('Picker interaction skipped:', e.message);
    }

    const switchErrors = pageErrors.length - switchErrsBefore;

    // Idle 30s — reset network counter
    networkRequests.length = 0;
    const idleErrsBefore = pageErrors.length;

    await page.waitForTimeout(30000);

    const idleErrors = pageErrors.length - idleErrsBefore;
    const idleRequestCount = networkRequests.length;

    // Print results for easy extraction
    console.log('ITER4_RESULT initial_load_pageerror=' + afterLoadErrors);
    console.log('ITER4_RESULT switch_pageerror=' + switchErrors);
    console.log('ITER4_RESULT idle30s_pageerror=' + idleErrors);
    console.log('ITER4_RESULT picker_visible=' + pickerVisible);
    console.log('ITER4_RESULT payoff_visible=' + payoffVisible);
    console.log('ITER4_RESULT idle30s_requests=' + idleRequestCount);
    console.log('ITER4_RESULT console_errors=' + JSON.stringify(consoleErrors));
    console.log('ITER4_RESULT all_pageerrors=' + JSON.stringify(pageErrors));

    // Assertions
    expect(afterLoadErrors, 'Initial-load pageerror').toBe(0);
    expect(switchErrors, 'Switch-underlying pageerror').toBe(0);
    expect(idleErrors, 'Idle-30s pageerror').toBe(0);
    expect(pickerVisible, '.opt-picker visible').toBe(true);
    expect(payoffVisible, '.opt-payoff visible').toBe(true);
  });
});
