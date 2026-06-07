/**
 * Precise interaction test for /admin/derivatives using discovered selectors.
 * Uses #opt-und (.rbq-select-trigger) for underlying picker.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('/admin/derivatives precise interaction', () => {
  test.setTimeout(180_000);

  test('underlying picker + payoff chart render timing', async ({ page }) => {
    const consoleMsgs = [];
    const pageErrors  = [];
    const netLog      = [];

    page.on('console', msg => consoleMsgs.push({ type: msg.type(), text: msg.text(), ts: Date.now() }));
    page.on('pageerror', err => pageErrors.push({ msg: err.message, stack: err.stack, ts: Date.now() }));

    const reqStart = new Map();
    page.on('request', req => reqStart.set(req._requestId || req.url(), Date.now()));
    page.on('response', async resp => {
      const url   = resp.url();
      const start = reqStart.get(resp.request()._requestId || url) ?? Date.now();
      let bytes = 0;
      try { bytes = (await resp.body().catch(() => Buffer.alloc(0))).length; } catch (_) {}
      netLog.push({ url, status: resp.status(), startMs: start, durMs: Date.now() - start, bytes });
    });

    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );

    netLog.length = 0;

    // ── Load page ────────────────────────────────────────────────────────────
    const t0 = Date.now();
    await page.goto('/admin/derivatives', { waitUntil: 'networkidle', timeout: 90_000 });
    const loadMs = Date.now() - t0;
    console.log(`\n  Load to networkidle: ${loadMs}ms`);

    // ── Confirm each_key_duplicate ────────────────────────────────────────────
    const eachKeyErrs = pageErrors.filter(e =>
      e.msg.includes('each_key_duplicate') || e.stack?.includes('each_key_duplicate')
    );
    console.log(`\n  each_key_duplicate thrown: ${eachKeyErrs.length > 0 ? 'YES — CONFIRMED BUG' : 'NO'}`);
    if (eachKeyErrs.length > 0) {
      console.log('  Stack:', eachKeyErrs[0].stack?.slice(0, 500));
    }

    // ── Check current underlying shown ────────────────────────────────────────
    const currentUnd = await page.locator('#opt-und .rbq-select-label, .rbq-select-label').first()
      .textContent().catch(() => '(not found)');
    console.log(`  Current underlying: "${currentUnd?.trim()}"`);

    // ── instruments fetch timing ───────────────────────────────────────────────
    const instrReq = netLog.find(r => r.url.includes('/api/instruments/'));
    console.log(`  /api/instruments/ timing: ${instrReq ? instrReq.durMs + 'ms' : 'NOT CALLED (cached)'}`);
    console.log(`  /api/instruments/ bytes: ${instrReq ? (instrReq.bytes/1024).toFixed(0) + 'KB' : 'N/A'}`);

    // ── Pick underlyings using correct selector ──────────────────────────────
    const underlyings = [
      { sym: 'NIFTY', expected: /NIFTY/ },
      { sym: 'BANKNIFTY', expected: /BANKNIFTY/ },
      { sym: 'CRUDEOIL', expected: /CRUDEOIL/ },
    ];

    console.log('\n  ── UNDERLYING PICK TIMING ──');

    for (const { sym, expected } of underlyings) {
      // Set up analytics response listener BEFORE clicking
      const analyticsResponsePromise = page.waitForResponse(
        r => r.url().includes('strategy-analytics'),
        { timeout: 20_000 }
      ).catch(() => null);

      const t1 = Date.now();

      // Click the underlying trigger to open dropdown
      await page.locator('#opt-und').click();
      await page.waitForTimeout(200); // dropdown animation

      // Find option in the open listbox
      const listbox = page.locator('[role="listbox"], .rbq-select-list, .rbq-select-options').first();
      const listboxVisible = await listbox.isVisible({ timeout: 3000 }).catch(() => false);
      console.log(`  [${sym}] listbox visible: ${listboxVisible}`);

      if (listboxVisible) {
        const option = listbox.locator('li, [role="option"]').filter({ hasText: new RegExp(`^${sym}`, 'i') }).first();
        const optVisible = await option.isVisible({ timeout: 2000 }).catch(() => false);
        console.log(`  [${sym}] option visible: ${optVisible}`);

        if (optVisible) {
          await option.click();
        } else {
          // Dump options for diagnosis
          const allOpts = await listbox.locator('li, [role="option"]').allTextContents().catch(() => []);
          console.log(`  [${sym}] available options: ${allOpts.slice(0, 10).join(', ')}`);
          // Close dropdown
          await page.keyboard.press('Escape');
        }
      } else {
        // Dump whatever opened
        const dropdownHtml = await page.locator('[role="listbox"], .rbq-dropdown, .select-dropdown').first()
          .innerHTML().catch(() => '(nothing opened)');
        console.log(`  [${sym}] dropdown HTML: ${dropdownHtml.slice(0, 300)}`);
        await page.keyboard.press('Escape');
      }

      // Wait for analytics response or timeout
      const analyticsResp = await analyticsResponsePromise;
      const dur = Date.now() - t1;

      // Check payoff chart appeared
      const chartVisible = await page.locator('.payoff-svg-stack, svg[class*="payoff"], .chart-svg, canvas').first()
        .isVisible({ timeout: 5000 }).catch(() => false);

      // Check label changed
      const newLabel = await page.locator('#opt-und .rbq-select-label, .rbq-select-label').first()
        .textContent().catch(() => '');

      let analyticsMs = -1;
      let analyticsStatus = -1;
      if (analyticsResp) {
        analyticsMs = dur;
        analyticsStatus = analyticsResp.status();
      }

      console.log(`  [${sym}] total: ${dur}ms | analytics: ${analyticsMs}ms (${analyticsStatus}) | chart: ${chartVisible} | label: "${newLabel?.trim().slice(0,30)}"`);

      // Wait for next pick
      await page.waitForTimeout(500);
    }

    // ── Hamburger (mobile nav) ────────────────────────────────────────────────
    console.log('\n  ── HAMBURGER NAV ──');
    const hamburger = page.locator('button[aria-label*="navigation"], button[aria-label*="menu"], .nav-hamburger, button:has(svg)').first();
    // On desktop the hamburger is hidden (lg:hidden), so check viewport
    const viewport = page.viewportSize();
    console.log(`  Viewport: ${viewport?.width}x${viewport?.height}`);
    // On 1400px desktop, hamburger should be hidden
    const hamburgerVisible = await hamburger.isVisible().catch(() => false);
    console.log(`  Hamburger visible at ${viewport?.width}px: ${hamburgerVisible}`);

    // ── WS reconnect storm ────────────────────────────────────────────────────
    const wsErrors = consoleMsgs.filter(m => m.type === 'error' && m.text.includes('WebSocket'));
    const wsBy405  = wsErrors.filter(m => m.text.includes('405'));
    console.log(`\n  ── WEBSOCKET STORM ──`);
    console.log(`  Total WS errors: ${wsErrors.length}`);
    console.log(`  WS 405 errors:   ${wsBy405.length}`);
    // Measure inter-error interval
    if (wsErrors.length > 2) {
      const times = wsErrors.map(e => e.ts);
      const intervals = [];
      for (let i = 1; i < times.length; i++) intervals.push(times[i] - times[i-1]);
      const avgInterval = intervals.reduce((s,v) => s+v, 0) / intervals.length;
      console.log(`  WS avg reconnect interval: ${avgInterval.toFixed(0)}ms`);
      console.log(`  WS reconnect rate: ~${(60000/avgInterval).toFixed(1)}/min`);
    }

    // ── console errors count ──────────────────────────────────────────────────
    const allConsoleErrors = consoleMsgs.filter(m => m.type === 'error' && !m.text.includes('WebSocket'));
    console.log(`\n  Non-WS console errors: ${allConsoleErrors.length}`);
    for (const e of allConsoleErrors) console.log(`    ${e.text.slice(0, 150)}`);

    // ── Slow requests ─────────────────────────────────────────────────────────
    const slowReqs = netLog.filter(r => r.durMs > 300).sort((a,b) => b.durMs - a.durMs);
    console.log(`\n  Slow requests (>300ms): ${slowReqs.length}`);
    for (const r of slowReqs.slice(0, 10)) {
      const path = r.url.replace(/https?:\/\/[^/]+/, '').slice(0, 80);
      console.log(`    ${r.durMs}ms ${r.status} ${path}`);
    }

    // Basic assertion: page loaded
    expect(loadMs).toBeLessThan(90_000);
  });
});
