/**
 * /admin/derivatives performance audit
 * Run with: PLAYWRIGHT_BASE_URL=https://dev.ramboq.com npx playwright test e2e/options_perf_audit.spec.js --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

test.describe('/admin/derivatives perf audit', () => {
  test.setTimeout(180_000);

  test('cold load + idle + interaction + nav stress', async ({ page, context }) => {
    // ── listeners ─────────────────────────────────────────────────────────────
    const consoleMsgs = [];
    const pageErrors  = [];
    const netLog      = [];   // { url, method, status, startMs, durMs, bytes }

    page.on('console', msg => {
      consoleMsgs.push({ type: msg.type(), text: msg.text(), ts: Date.now() });
    });
    page.on('pageerror', err => {
      pageErrors.push({ msg: err.message, stack: err.stack, ts: Date.now() });
    });

    // Network timing via response events
    const reqStart = new Map(); // requestId → startMs
    page.on('request', req => {
      reqStart.set(req.url(), Date.now());
    });
    page.on('response', async resp => {
      const url    = resp.url();
      const start  = reqStart.get(url) ?? Date.now();
      const dur    = Date.now() - start;
      let   bytes  = 0;
      try {
        const body = await resp.body().catch(() => Buffer.alloc(0));
        bytes = body.length;
      } catch (_) {}
      netLog.push({
        url,
        method: resp.request().method(),
        status: resp.status(),
        startMs: start,
        durMs: dur,
        bytes,
      });
    });

    // ── auth ──────────────────────────────────────────────────────────────────
    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );

    // ── Phase 1 — Cold load ───────────────────────────────────────────────────
    const t0 = Date.now();
    consoleMsgs.length = 0; pageErrors.length = 0; netLog.length = 0;

    await page.goto('/admin/derivatives', {
      waitUntil: 'load',
      timeout: 90_000,
    });

    const tLoad = Date.now();
    const loadMs = tLoad - t0;

    // Wait for first interactive: opt-picker visible + enabled
    await page.locator('.opt-picker, .page-picker, select, .custom-select').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    const tInteractive = Date.now();
    const interactiveMs = tInteractive - tLoad;

    // JS heap
    const heapAfterLoad = await page.evaluate(() =>
      (performance.memory?.usedJSHeapSize ?? 0) / (1024 * 1024)
    );

    // Total bytes
    const totalBytes = netLog.reduce((s, r) => s + r.bytes, 0) / 1024;

    // each_key_duplicate errors
    const eachKeyErrors = consoleMsgs.filter(m =>
      m.text.includes('each_key_duplicate') || m.text.includes('each-block')
    );

    // All console errors
    const consoleErrors = consoleMsgs.filter(m => m.type === 'error');

    console.log('\n\n═══════════════════════════════════════════');
    console.log('  PHASE 1 — COLD LOAD');
    console.log('═══════════════════════════════════════════');
    console.log(`  load event:       ${loadMs} ms`);
    console.log(`  first interactive:${interactiveMs} ms (after load)`);
    console.log(`  total bytes:      ${totalBytes.toFixed(1)} KB`);
    console.log(`  JS heap:          ${heapAfterLoad.toFixed(1)} MB`);
    console.log(`  each_key errors:  ${eachKeyErrors.length}`);
    console.log(`  console errors:   ${consoleErrors.length}`);

    // Group net requests by endpoint
    const byEndpoint = {};
    for (const r of netLog) {
      let key = r.url.replace(/https?:\/\/[^/]+/, '');
      // strip query strings for grouping
      const qi = key.indexOf('?');
      if (qi > -1) key = key.slice(0, qi) + ' (+ query)';
      if (!byEndpoint[key]) byEndpoint[key] = { count: 0, totalMs: 0, statuses: [] };
      byEndpoint[key].count++;
      byEndpoint[key].totalMs += r.durMs;
      byEndpoint[key].statuses.push(r.status);
    }

    console.log('\n  Network requests (cold load):');
    for (const [ep, v] of Object.entries(byEndpoint).sort((a,b) => b[1].totalMs - a[1].totalMs)) {
      const avgMs = (v.totalMs / v.count).toFixed(0);
      const statuses = [...new Set(v.statuses)].join(',');
      console.log(`    [${String(v.count).padStart(3)}x] ${avgMs.padStart(5)}ms avg  ${statuses.padStart(4)}  ${ep}`);
    }

    if (consoleErrors.length) {
      console.log('\n  CONSOLE ERRORS on cold load:');
      for (const e of consoleErrors) console.log('   ', e.type, '|', e.text.slice(0, 200));
    }
    if (eachKeyErrors.length) {
      console.log('\n  EACH_KEY_DUPLICATE errors:');
      for (const e of eachKeyErrors) console.log('   ', e.text.slice(0, 200));
    }

    // ── Phase 2 — Idle 30s observation ────────────────────────────────────────
    const heapPreIdle = heapAfterLoad;
    const netBefore   = netLog.length;
    const idleStart   = Date.now();

    // Observe for 30 s
    await page.waitForTimeout(30_000);

    const idleDur  = (Date.now() - idleStart) / 1000;
    const heapPostIdle = await page.evaluate(() =>
      (performance.memory?.usedJSHeapSize ?? 0) / (1024 * 1024)
    );
    const idleRequests = netLog.slice(netBefore);
    const heapGrowth   = ((heapPostIdle - heapPreIdle) / idleDur * 60).toFixed(2);

    // Group idle requests
    const idleByEp = {};
    for (const r of idleRequests) {
      let key = r.url.replace(/https?:\/\/[^/]+/, '');
      const qi = key.indexOf('?');
      if (qi > -1) key = key.slice(0, qi) + ' (+ query)';
      if (!idleByEp[key]) idleByEp[key] = { count: 0, totalMs: 0 };
      idleByEp[key].count++;
      idleByEp[key].totalMs += r.durMs;
    }

    // Detect duplicates (same full URL hit >3 times)
    const urlCounts = {};
    for (const r of idleRequests) {
      urlCounts[r.url] = (urlCounts[r.url] || 0) + 1;
    }
    const duplicates = Object.entries(urlCounts).filter(([,c]) => c > 3);

    console.log('\n\n═══════════════════════════════════════════');
    console.log('  PHASE 2 — IDLE 30s');
    console.log('═══════════════════════════════════════════');
    console.log(`  requests fired:   ${idleRequests.length}`);
    console.log(`  heap growth:      ${heapGrowth} MB/min`);
    console.log(`  duplicate URLs:   ${duplicates.length}`);

    console.log('\n  Idle polling breakdown:');
    for (const [ep, v] of Object.entries(idleByEp).sort((a,b) => b[1].count - a[1].count)) {
      const avgMs = (v.totalMs / v.count).toFixed(0);
      console.log(`    [${String(v.count).padStart(3)}x] ${avgMs.padStart(5)}ms avg  ${ep}`);
    }
    if (duplicates.length) {
      console.log('\n  DUPLICATE URLS (>3 hits in 30s):');
      for (const [url, cnt] of duplicates) console.log(`    [${cnt}x] ${url.slice(0, 120)}`);
    }

    // ── Phase 3 — Interaction stress ──────────────────────────────────────────
    console.log('\n\n═══════════════════════════════════════════');
    console.log('  PHASE 3 — INTERACTION STRESS');
    console.log('═══════════════════════════════════════════');

    // Helper to wait for network quiet (no new requests for 1.5s)
    async function waitForNetQuiet(ms = 1500) {
      let last = netLog.length;
      const deadline = Date.now() + 8000;
      while (Date.now() < deadline) {
        await page.waitForTimeout(ms);
        if (netLog.length === last) break;
        last = netLog.length;
      }
    }

    // Find picker
    const pickerSel = '.opt-picker select, .opt-picker .custom-select, select[name="underlying"], .underlying-picker';

    // 3a — open picker
    const tPickerOpen = Date.now();
    const pickerEl = page.locator('.opt-picker').first();
    const pickerVisible = await pickerEl.isVisible().catch(() => false);

    let pickerOpenMs = -1;
    if (pickerVisible) {
      await pickerEl.click().catch(() => {});
      pickerOpenMs = Date.now() - tPickerOpen;
      console.log(`  picker click:     ${pickerOpenMs} ms`);
    } else {
      console.log('  picker:           NOT VISIBLE — checking page state');
      const html = await page.locator('body').innerHTML().catch(() => '');
      const hasError = html.includes('error') || html.includes('Error');
      console.log(`  page has "error": ${hasError}`);
    }

    // 3b — pick NIFTY
    const underlyings = ['NIFTY', 'BANKNIFTY', 'CRUDEOIL'];
    const interactionTimings = [];

    for (const sym of underlyings) {
      const netBefore2 = netLog.length;
      const t1 = Date.now();

      // Try native select first, then custom
      const nativeSelect = page.locator('select').filter({ hasText: sym }).first();
      const customOpt = page.locator('.opt-underlying, .underlying-opt').filter({ hasText: sym }).first();

      // Find underlying selector
      const allSelects = await page.locator('select').all();
      let picked = false;
      for (const sel of allSelects) {
        const opts = await sel.locator('option').allTextContents().catch(() => []);
        if (opts.some(o => o.includes(sym))) {
          await sel.selectOption({ label: opts.find(o => o.includes(sym)) || sym }).catch(() => {});
          picked = true;
          break;
        }
      }

      if (!picked) {
        // Try custom select — look for a listbox / dropdown button
        const trigger = page.locator(`[data-value="${sym}"], .select-option:has-text("${sym}"), li:has-text("${sym}")`).first();
        const triggerVisible = await trigger.isVisible().catch(() => false);
        if (triggerVisible) {
          await trigger.click().catch(() => {});
          picked = true;
        }
      }

      await waitForNetQuiet(1500);
      const dur = Date.now() - t1;
      const newReqs = netLog.slice(netBefore2);
      const strategyReqs = newReqs.filter(r => r.url.includes('strategy-analytics') || r.url.includes('analytics'));

      // Check chart rendered
      const chartVisible = await page.locator('.payoff-svg-stack, svg.payoff-chart, .chart-svg, canvas').first()
        .isVisible({ timeout: 5000 }).catch(() => false);
      const errorVisible = await page.locator('.error, [role="alert"]').first()
        .isVisible({ timeout: 1000 }).catch(() => false);

      interactionTimings.push({ sym, dur, picked, chartVisible, errorVisible, strategyReqs: strategyReqs.length });
      console.log(`  → ${sym}: ${dur}ms | picked=${picked} | chart=${chartVisible} | error=${errorVisible} | analytics_reqs=${strategyReqs.length}`);
    }

    // 3c — hamburger
    const tHamburger = Date.now();
    const hamburger = page.locator('[aria-label="Open navigation"], button.hamburger, .nav-hamburger, button[aria-expanded]').first();
    const hamburgerVisible = await hamburger.isVisible().catch(() => false);
    let hamburgerMs = -1;
    let drawerLinkClickable = false;

    if (hamburgerVisible) {
      await hamburger.click();
      hamburgerMs = Date.now() - tHamburger;
      // Check drawer has links
      const drawerLink = page.locator('nav a, .nav-drawer a, .mobile-nav a').first();
      drawerLinkClickable = await drawerLink.isVisible({ timeout: 3000 }).catch(() => false);
      // Close drawer
      await page.keyboard.press('Escape').catch(() => {});
    }
    console.log(`  hamburger:        ${hamburgerMs}ms | drawer links=${drawerLinkClickable}`);

    // ── Phase 4 — Navigation stress ───────────────────────────────────────────
    console.log('\n\n═══════════════════════════════════════════');
    console.log('  PHASE 4 — NAVIGATION STRESS (3 rounds)');
    console.log('═══════════════════════════════════════════');

    const heapPreNav = await page.evaluate(() =>
      (performance.memory?.usedJSHeapSize ?? 0) / (1024 * 1024)
    );

    const navRounds = [];
    for (let i = 0; i < 3; i++) {
      const tToOrders = Date.now();
      await page.goto('/orders', { waitUntil: 'load', timeout: 30_000 }).catch(() => {});
      const toOrdersMs = Date.now() - tToOrders;

      const tToOptions = Date.now();
      await page.goto('/admin/derivatives', { waitUntil: 'load', timeout: 30_000 }).catch(() => {});
      const toOptionsMs = Date.now() - tToOptions;

      // Picker still works?
      const pickerWorking = await page.locator('.opt-picker, select').first()
        .isVisible({ timeout: 5000 }).catch(() => false);

      navRounds.push({ toOrdersMs, toOptionsMs, pickerWorking });
      console.log(`  Round ${i+1}: /options→/orders=${toOrdersMs}ms | /orders→/options=${toOptionsMs}ms | picker=${pickerWorking}`);
    }

    const heapPostNav = await page.evaluate(() =>
      (performance.memory?.usedJSHeapSize ?? 0) / (1024 * 1024)
    );
    const navDurMin = (navRounds.length * 2 * 5) / 60; // rough estimate
    const memGrowthNav = (heapPostNav - heapPreNav).toFixed(1);

    console.log(`  heap pre-nav:  ${heapPreNav.toFixed(1)} MB`);
    console.log(`  heap post-nav: ${heapPostNav.toFixed(1)} MB`);
    console.log(`  heap growth:   +${memGrowthNav} MB`);

    // ── Slow operations summary ────────────────────────────────────────────────
    console.log('\n\n═══════════════════════════════════════════');
    console.log('  SLOW OPERATIONS (>500ms)');
    console.log('═══════════════════════════════════════════');
    const slowReqs = netLog.filter(r => r.durMs > 500);
    for (const r of slowReqs.sort((a,b) => b.durMs - a.durMs).slice(0, 15)) {
      console.log(`  ${r.durMs}ms  ${r.status}  ${r.url.replace(/https?:\/\/[^/]+/, '').slice(0, 100)}`);
    }

    // ── Console errors full dump ───────────────────────────────────────────────
    const allErrors = consoleMsgs.filter(m => m.type === 'error' || m.type === 'warning');
    if (allErrors.length) {
      console.log('\n\n═══════════════════════════════════════════');
      console.log('  ALL CONSOLE ERRORS/WARNINGS');
      console.log('═══════════════════════════════════════════');
      for (const e of allErrors.slice(0, 30)) {
        console.log(`  [${e.type}] ${e.text.slice(0, 250)}`);
      }
    }

    if (pageErrors.length) {
      console.log('\n\n═══════════════════════════════════════════');
      console.log('  PAGE ERRORS (uncaught JS exceptions)');
      console.log('═══════════════════════════════════════════');
      for (const e of pageErrors) {
        console.log(`  ${e.msg.slice(0, 300)}`);
        if (e.stack) console.log('  Stack:', e.stack.slice(0, 400));
      }
    }

    // ── Trace save ────────────────────────────────────────────────────────────
    // Trace is retained via playwright config; save a copy to /tmp
    console.log('\n\n  [Trace saved via retain-on-failure config to test-results/]');
    console.log('\n═══════════════════════════════════════════\n');

    // Basic sanity assertions (soft — we want the report even on failures)
    expect(loadMs).toBeLessThan(90_000);   // must load within 90s
  });
});
