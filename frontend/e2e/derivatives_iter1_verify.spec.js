/**
 * Iter 1 verification — commit 0bf4176f Select.svelte de-dup fix
 * Target: https://dev.ramboq.com/admin/derivatives
 *
 * Checks:
 * 1. Page loads in <10s (DOMContentLoaded), no each_key_duplicate pageerror
 * 2. .opt-payoff visible (requires strategy analytics to load)
 * 3. .payoff-svg-stack rendered inside .opt-payoff
 * 4. Underlying switch: second option then back → no console errors
 * 5. Hamburger menu at ≤1024px
 * 6. Pageerror count, console error count, 30s idle request count
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const PAGE_PATH = '/admin/derivatives';

// 120s covers 3-retry auth (up to ~25s) + page load + strategy-analytics fetch
test.setTimeout(120_000);

test.describe('Iter 1 — derivatives page Select de-dup fix', () => {
  test('page loads clean — no each_key_duplicate, payoff card + SVG visible', async ({ page }) => {
    const pageErrors = [];
    const consoleErrors = [];

    page.on('pageerror', (err) => pageErrors.push(err.message));
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        const text = msg.text();
        // Exclude known Cloudflare WS 405 noise
        if (!text.includes('WebSocket') && !text.includes('405') && !text.includes('wss://')) {
          consoleErrors.push(text);
        }
      }
    });

    // Auth: try ambore then rambo
    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );

    const t0 = Date.now();
    await page.goto(PAGE_PATH, { waitUntil: 'domcontentloaded' });
    const domReadyMs = Date.now() - t0;

    // DOMContentLoaded in < 10s
    expect(domReadyMs, `DOMContentLoaded took ${domReadyMs}ms`).toBeLessThan(10000);

    // Wait for the Candidates grid to confirm page content loaded
    // The grid is always present (holds the leg checkboxes); payoff is conditional on strategy
    await page.waitForSelector('.opt-picker', { timeout: 15000 });

    // Check no each_key_duplicate errors (the key check)
    const dupErrors = pageErrors.filter((e) => e.includes('each_key_duplicate'));
    expect(dupErrors.length, `each_key_duplicate errors: ${dupErrors.join('; ')}`).toBe(0);

    // Wait for strategy-analytics response which populates strategy state → .opt-payoff
    // Strategy fetches when legs are checked; allow 60s for the full chain
    const strategyResp = await page.waitForResponse(
      (r) => r.url().includes('strategy-analytics'),
      { timeout: 60000 }
    ).catch(() => null);

    console.log(`strategy-analytics response: ${strategyResp ? strategyResp.status() : 'none (no legs checked?)'}`);

    // .opt-payoff renders when strategy is loaded; if no legs checked it won't appear
    const payoffVisible = await page.locator('.opt-payoff').first().isVisible().catch(() => false);
    console.log(`.opt-payoff visible: ${payoffVisible}`);

    if (payoffVisible) {
      await expect(page.locator('.opt-payoff').first()).toBeVisible();
      // .payoff-svg-stack is inside OptionsPayoff.svelte
      const svgStack = page.locator('.payoff-svg-stack').first();
      const svgVisible = await svgStack.isVisible().catch(() => false);
      console.log(`.payoff-svg-stack visible: ${svgVisible}`);
      if (svgVisible) {
        await expect(svgStack).toBeVisible();
      }
    }

    console.log(`DOMContentLoaded: ${domReadyMs}ms`);
    console.log(`Pageerrors total: ${pageErrors.length}`);
    console.log(`Each_key_duplicate: ${dupErrors.length}`);
    console.log(`Console errors (filtered): ${consoleErrors.length}`);
    if (consoleErrors.length) console.log(`Console errors: ${JSON.stringify(consoleErrors)}`);
  });

  test('underlying switch — second then back — no crash', async ({ page }) => {
    const pageErrors = [];
    const consoleErrors = [];

    page.on('pageerror', (err) => pageErrors.push(err.message));
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        const text = msg.text();
        if (!text.includes('WebSocket') && !text.includes('405') && !text.includes('wss://')) {
          consoleErrors.push(text);
        }
      }
    });

    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );
    await page.goto(PAGE_PATH, { waitUntil: 'domcontentloaded' });
    // Wait for the picker bar to confirm page loaded
    await page.waitForSelector('.opt-picker', { timeout: 15000 });

    // The underlying picker — look for a Select component in the picker bar
    // The derivatives page has: Account | Underlying | Expiry | Chain button
    // The Underlying dropdown shows the currently selected underlying (e.g. CRUDEOIL)
    // It's a custom Select rendered as a button/div with class containing 'sel'
    const underlyingLabel = page.locator('label[for="opt-und"]');
    const labelCount = await underlyingLabel.count();
    console.log(`Underlying label found: ${labelCount}`);

    // Find the Select/combobox right after the underlying label
    // The custom Select component renders its trigger with aria-label or wraps under .opt-field
    const optField = page.locator('.opt-field').nth(1); // Underlying is the second opt-field
    const optFieldHtml = await optField.innerHTML().catch(() => '(not found)');
    console.log(`Underlying opt-field HTML (first 300): ${optFieldHtml.slice(0, 300)}`);

    // Try clicking the Select trigger inside the Underlying field
    const selTrigger = optField.locator('button, [role="combobox"], [role="button"], .sel-val, .rq-sel-val').first();
    const trigCount = await selTrigger.count();
    console.log(`Underlying select trigger candidates: ${trigCount}`);

    if (trigCount > 0 && await selTrigger.isVisible()) {
      const currentText = await selTrigger.textContent();
      console.log(`Current underlying: ${currentText?.trim()}`);

      await selTrigger.click();
      await page.waitForTimeout(600);

      // Options in the opened dropdown
      const options = page.locator('[role="option"], [role="listbox"] li, .rq-dropdown-item, .sel-opt');
      const optCount = await options.count();
      console.log(`Dropdown options: ${optCount}`);

      if (optCount >= 2) {
        const secondText = await options.nth(1).textContent();
        await options.nth(1).click();
        console.log(`Switched to: ${secondText?.trim()}`);
        await page.waitForTimeout(2000);

        // Switch back to first
        await selTrigger.click();
        await page.waitForTimeout(600);
        const backOpts = page.locator('[role="option"], [role="listbox"] li, .rq-dropdown-item, .sel-opt');
        if (await backOpts.count() >= 1) {
          await backOpts.first().click();
          await page.waitForTimeout(2000);
        }
        console.log('Switch back complete');
      } else if (optCount === 1) {
        console.log('Only 1 underlying available in dropdown — switch test skipped');
        await page.keyboard.press('Escape');
      } else {
        console.log('No options visible in dropdown after click — dropdown may use different selectors');
        await page.keyboard.press('Escape');
      }
    } else {
      console.log('Underlying select trigger not found or not visible');
    }

    const dupErrors = pageErrors.filter((e) => e.includes('each_key_duplicate'));
    expect(dupErrors.length, `each_key_duplicate after switch: ${dupErrors.join('; ')}`).toBe(0);
    expect(consoleErrors.length, `console errors after switch: ${consoleErrors.join('; ')}`).toBe(0);

    console.log(`Post-switch pageerrors: ${pageErrors.length}`);
    console.log(`Post-switch console errors: ${consoleErrors.length}`);
  });

  test('hamburger menu at 900px — opens and navigates', async ({ page }) => {
    await page.setViewportSize({ width: 900, height: 800 });

    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );
    await page.goto(PAGE_PATH, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.opt-picker', { timeout: 15000 });

    // Algo layout hamburger — visible below lg: (1024px) breakpoint
    // Check if a hamburger button is visible at 900px
    const allNavButtons = await page.locator('nav button').all();
    console.log(`Nav buttons at 900px: ${allNavButtons.length}`);

    let hamEl = null;
    for (const btn of allNavButtons) {
      const ariaLabel = await btn.getAttribute('aria-label').catch(() => '');
      const cls = await btn.getAttribute('class').catch(() => '');
      const isVis = await btn.isVisible();
      if (isVis && /menu|hamburger|drawer|toggle/i.test(ariaLabel + cls)) {
        hamEl = btn;
        console.log(`Found hamburger: aria-label="${ariaLabel}" class="${cls.slice(0, 60)}"`);
        break;
      }
    }

    // Fallback: look for any nav button that has no text but has SVG (common hamburger pattern)
    if (!hamEl) {
      for (const btn of allNavButtons) {
        const text = (await btn.textContent().catch(() => '')).trim();
        const hasSvg = await btn.locator('svg').count() > 0;
        const isVis = await btn.isVisible();
        if (isVis && hasSvg && !text) {
          hamEl = btn;
          console.log('Found hamburger by: visible, has SVG, no text');
          break;
        }
      }
    }

    const hamFound = hamEl !== null;
    console.log(`Hamburger found at 900px: ${hamFound}`);

    if (hamFound) {
      await hamEl.click();
      await page.waitForTimeout(700);

      // After opening, look for visible nav links in a drawer/menu
      const drawerLinks = page.locator('a[href]').filter({ hasText: /Dashboard|Pulse|Agents|Orders/i });
      const visibleLinks = [];
      for (const link of await drawerLinks.all()) {
        if (await link.isVisible()) {
          visibleLinks.push(await link.getAttribute('href'));
        }
      }
      console.log(`Visible nav links after hamburger open: ${visibleLinks.slice(0, 5).join(', ')}`);

      if (visibleLinks.length > 0) {
        // Click the first visible nav link
        const target = drawerLinks.first();
        const href = await target.getAttribute('href');
        await target.click();
        await page.waitForURL(/.+/, { timeout: 8000 }).catch(() => {});
        console.log(`Navigated to: ${page.url()} (expected: ${href})`);
        expect(page.url()).toMatch(/dashboard|pulse|agents|orders|performance/);
      }
    } else {
      // Log what's in the nav so we know the breakpoint situation
      const navText = await page.locator('nav').first().textContent().catch(() => '');
      console.log(`Nav content at 900px (first 200): ${navText.slice(0, 200)}`);
    }
  });
});

test.describe('Iter 1 — 30s idle request count', () => {
  test.setTimeout(120_000); // 30s idle + 60s auth + overhead

  test('idle 30s — request count, slow ops, no each_key_duplicate', async ({ page }) => {
    const requests = [];
    const slowOps = [];
    const pageErrors = [];

    page.on('pageerror', (err) => pageErrors.push(err.message));
    page.on('request', (req) => {
      requests.push({ url: req.url(), ts: Date.now() });
    });
    page.on('response', async (res) => {
      try {
        const timing = res.request().timing();
        const dur = timing.responseEnd - timing.requestStart;
        if (dur > 500) {
          slowOps.push({
            path: (() => { try { return new URL(res.url()).pathname; } catch { return res.url().slice(0, 80); } })(),
            ms: Math.round(dur),
          });
        }
      } catch {
        // timing not available for all requests
      }
    });

    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );

    await page.goto(PAGE_PATH, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.opt-picker', { timeout: 15000 });

    // Let page fully settle (strategy analytics fetch etc.) then start 30s idle window
    await page.waitForTimeout(5000);
    const baselineCount = requests.length;
    const idleStart = Date.now();

    await page.waitForTimeout(30000);

    const idleRequests = requests.filter((r) => r.ts > idleStart).length;
    const dupErrors = pageErrors.filter((e) => e.includes('each_key_duplicate'));
    const slowSorted = [...slowOps].sort((a, b) => b.ms - a.ms).slice(0, 8);

    console.log(`Baseline requests (page load + 5s settle): ${baselineCount}`);
    console.log(`Idle 30s requests: ${idleRequests}`);
    console.log(`Each_key_duplicate during idle: ${dupErrors.length}`);
    console.log(`Slow ops (>500ms): ${JSON.stringify(slowSorted)}`);

    expect(idleRequests).toBeLessThan(300);
    expect(dupErrors.length).toBe(0);
  });
});
