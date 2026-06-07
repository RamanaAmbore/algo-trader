/**
 * DOM probe for /admin/derivatives — discover actual picker structure
 * and measure strategy-analytics timing precisely.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('/admin/derivatives DOM probe', () => {
  test.setTimeout(120_000);

  test('discover picker DOM and measure analytics timing', async ({ page }) => {
    const consoleMsgs = [];
    const pageErrors  = [];
    const netLog      = [];

    page.on('console', msg => consoleMsgs.push({ type: msg.type(), text: msg.text() }));
    page.on('pageerror', err => pageErrors.push({ msg: err.message, stack: err.stack }));
    page.on('response', async resp => {
      const url = resp.url();
      const start = Date.now();
      netLog.push({ url, status: resp.status(), ts: start, dur: 0 });
    });
    page.on('requestfinished', req => {
      const entry = netLog.find(r => r.url === req.url() && r.dur === 0);
      if (entry) entry.dur = Date.now() - entry.ts;
    });

    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );

    netLog.length = 0; consoleMsgs.length = 0; pageErrors.length = 0;

    await page.goto('/admin/derivatives', { waitUntil: 'networkidle', timeout: 60_000 });

    // ── 1. Discover actual DOM structure of picker area ────────────────────
    const pickerHtml = await page.locator('.opt-picker, .page-picker, .picker-bar, [class*="picker"], [class*="opt-"]').first()
      .innerHTML().catch(() => '(not found)');

    console.log('\n── PICKER AREA HTML (first 1000 chars) ──');
    console.log(pickerHtml.slice(0, 1000));

    // All select elements on the page
    const selects = await page.locator('select').all();
    console.log(`\n── SELECT ELEMENTS: ${selects.length} ──`);
    for (const sel of selects) {
      const name  = await sel.getAttribute('name').catch(() => '');
      const id    = await sel.getAttribute('id').catch(() => '');
      const cls   = await sel.getAttribute('class').catch(() => '');
      const opts  = await sel.locator('option').allTextContents().catch(() => []);
      console.log(`  select[name=${name} id=${id} class=${cls.slice(0,50)}]: ${opts.length} options: ${opts.slice(0,5).join(', ')}`);
    }

    // All custom select components
    const customSelects = await page.locator('[role="listbox"], [role="combobox"], .custom-select, .select-trigger, .select-wrapper').all();
    console.log(`\n── CUSTOM SELECTS: ${customSelects.length} ──`);
    for (const cs of customSelects) {
      const cls  = await cs.getAttribute('class').catch(() => '');
      const text = await cs.textContent().catch(() => '');
      console.log(`  ${cls.slice(0,60)}: "${text.slice(0,60).trim()}"`);
    }

    // Full opt-picker area
    const optPicker = await page.locator('.opt-picker').count();
    console.log(`\n── .opt-picker elements: ${optPicker} ──`);

    if (optPicker > 0) {
      const fullHtml = await page.locator('.opt-picker').first().innerHTML().catch(() => '');
      console.log('Full opt-picker HTML:');
      console.log(fullHtml.slice(0, 2000));
    }

    // ── 2. Get the page title and visible text to confirm it loaded ────────
    const title = await page.title();
    const h1s   = await page.locator('h1, .page-title-chip, .algo-title-group').allTextContents().catch(() => []);
    console.log(`\n── PAGE STATE ──`);
    console.log(`  title: ${title}`);
    console.log(`  headings: ${h1s.join(' | ')}`);

    // ── 3. Find the underlying underlying-selector however it's named ──────
    const allInputs = await page.locator('input, select, button').all();
    console.log(`\n── INTERACTIVE ELEMENTS: ${allInputs.length} total ──`);

    // Look for anything that might be an underlying picker
    const buttons = await page.locator('button').all();
    const relevantBtns = [];
    for (const btn of buttons) {
      const text = await btn.textContent().catch(() => '');
      const cls  = await btn.getAttribute('class').catch(() => '');
      if (/NIFTY|BANKNIFTY|underlying|picker|chain|option/i.test(text + cls)) {
        relevantBtns.push({ text: text.trim().slice(0, 60), cls: cls.slice(0, 60) });
      }
    }
    console.log(`\n── RELEVANT BUTTONS: ${relevantBtns.length} ──`);
    for (const b of relevantBtns) console.log(`  "${b.text}" [${b.cls}]`);

    // ── 4. Screenshot to see actual page state ──────────────────────────────
    await page.screenshot({ path: '/tmp/options_page_state.png', fullPage: false });
    console.log('\n  Screenshot saved: /tmp/options_page_state.png');

    // ── 5. each_key_duplicate — is it still there? ──────────────────────────
    const eachKeyErrors = pageErrors.filter(e =>
      e.msg.includes('each_key_duplicate') || e.stack?.includes('each_key_duplicate')
    );
    console.log(`\n── each_key_duplicate errors: ${eachKeyErrors.length} ──`);
    for (const e of eachKeyErrors) {
      console.log('  ', e.msg.slice(0, 300));
    }

    // ── 6. Measure strategy-analytics timing precisely ─────────────────────
    //  Wait for page to fully settle first
    await page.waitForTimeout(3000);

    // Find any underlying-related dropdown — try the SelectButton pattern
    // The page uses custom Select components — find the select-btn or similar
    const selectBtns = await page.locator('.select-btn, .select-button, .sel-trigger, [data-select]').all();
    console.log(`\n── CUSTOM SELECT BUTTONS: ${selectBtns.length} ──`);
    for (const btn of selectBtns) {
      const text = await btn.textContent().catch(() => '');
      const cls  = await btn.getAttribute('class').catch(() => '');
      console.log(`  "${text.trim().slice(0,60)}" [${cls.slice(0,60)}]`);
    }

    // ── 7. Try to actually pick NIFTY via any available mechanism ──────────
    console.log('\n── ATTEMPTING NIFTY PICK ──');

    // Try approach: find li or option containing NIFTY text
    let strategyT0 = 0;
    let strategyDur = -1;

    // First click any visible select-like element to open
    const trigger = page.locator('button, .select-btn, [role="button"]')
      .filter({ hasText: /NIFTY|underlying|Select/i }).first();
    const triggerVisible = await trigger.isVisible().catch(() => false);
    console.log(`  NIFTY trigger visible: ${triggerVisible}`);

    if (triggerVisible) {
      // Set up response listener for strategy-analytics
      const analyticsPromise = page.waitForResponse(
        r => r.url().includes('strategy-analytics'),
        { timeout: 15000 }
      ).catch(() => null);

      strategyT0 = Date.now();
      await trigger.click();
      await page.waitForTimeout(500);

      // Look for options in opened dropdown
      const opts = await page.locator('li, [role="option"]')
        .filter({ hasText: 'NIFTY' }).all();
      console.log(`  Options in dropdown after click: ${opts.length}`);

      if (opts.length > 0) {
        await opts[0].click();
        const analyticsResp = await analyticsPromise;
        strategyDur = Date.now() - strategyT0;
        if (analyticsResp) {
          console.log(`  strategy-analytics: ${strategyDur}ms status=${analyticsResp.status()}`);
        } else {
          console.log(`  strategy-analytics: no response in 15s (dur=${strategyDur}ms)`);
        }
      }
    } else {
      // Try clicking by text match broadly
      const niftyOpt = page.locator('li, option, [role="option"]').filter({ hasText: /^NIFTY$/ }).first();
      const niftyVisible = await niftyOpt.isVisible({ timeout: 2000 }).catch(() => false);
      console.log(`  NIFTY list option visible: ${niftyVisible}`);
    }

    // ── 8. WebSocket reconnection storm check ──────────────────────────────
    const wsErrors = consoleMsgs.filter(m => m.text.includes('WebSocket') && m.type === 'error');
    console.log(`\n── WebSocket errors: ${wsErrors.length} ──`);
    if (wsErrors.length > 0) {
      console.log('  Sample:', wsErrors[0].text.slice(0, 150));
    }

    // ── 9. instruments/ fetch — how long? ─────────────────────────────────
    const instrReqs = netLog.filter(r => r.url.includes('/instruments/'));
    console.log(`\n── /api/instruments/ requests: ${instrReqs.length} ──`);
    for (const r of instrReqs) console.log(`  ${r.dur}ms status=${r.status}`);

    // ── 10. strategy-analytics all calls ──────────────────────────────────
    const saReqs = netLog.filter(r => r.url.includes('strategy-analytics') || r.url.includes('analytics'));
    console.log(`\n── strategy-analytics calls: ${saReqs.length} ──`);
    for (const r of saReqs) console.log(`  ${r.dur}ms status=${r.status}`);
  });
});
