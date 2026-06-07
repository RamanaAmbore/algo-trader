/**
 * Iter 2 verification — commit b53f53e3
 * <svelte:boundary> wrap + candidates key tiebreaker
 * Target: https://dev.ramboq.com/admin/derivatives
 *
 * Checks:
 * 1. Initial load: count pageerror events
 * 2. Switch underlying twice → count pageerror events
 * 3. After 30s idle: count pageerror events
 * 4. .opt-payoff visible after load
 * 5. .payoff-svg-stack rendered
 * 6. Idle 30s request count + new console errors (excluding WS 405 + SSE 401)
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = 'https://dev.ramboq.com';
const PAGE_PATH = '/admin/derivatives';

test.use({ baseURL: BASE });
test.setTimeout(180_000);

/** Filter out known noise: WS 405, SSE 401 */
function isNoise(text) {
  return (
    text.includes('WebSocket') ||
    text.includes('405') ||
    text.includes('wss://') ||
    // SSE 401 timing
    (text.includes('401') && text.includes('stream'))
  );
}

test.describe('Iter 2 — svelte:boundary + candidates key tiebreaker', () => {
  test('initial load: pageerror count + payoff card + SVG visibility', async ({ page }) => {
    const pageErrors = [];
    const consoleErrors = [];
    const requests = [];

    page.on('pageerror', (err) => pageErrors.push(err.message));
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        const t = msg.text();
        if (!isNoise(t)) consoleErrors.push(t);
      }
    });
    page.on('request', (req) => requests.push(req.url()));

    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );

    const t0 = Date.now();
    await page.goto(PAGE_PATH, { waitUntil: 'domcontentloaded' });
    const domMs = Date.now() - t0;

    // Wait for picker bar — confirms page content rendered
    await page.waitForSelector('.opt-picker', { timeout: 20000 });

    // Snapshot initial-load errors
    const initialPageErrors = [...pageErrors];
    const initialConsoleErrors = [...consoleErrors];

    // Give strategy-analytics time to fire (auto-fetches when legs are available)
    const stratResp = await page
      .waitForResponse((r) => r.url().includes('strategy-analytics'), { timeout: 45000 })
      .catch(() => null);

    console.log(`DOM ready: ${domMs}ms`);
    console.log(`strategy-analytics: ${stratResp ? stratResp.status() : 'none'}`);

    // .opt-payoff visibility
    const payoffVisible = await page.locator('.opt-payoff').first().isVisible().catch(() => false);
    const svgVisible = payoffVisible
      ? await page.locator('.payoff-svg-stack').first().isVisible().catch(() => false)
      : false;

    console.log(`.opt-payoff visible: ${payoffVisible}`);
    console.log(`.payoff-svg-stack visible: ${svgVisible}`);
    console.log(`Initial pageerrors (${initialPageErrors.length}): ${JSON.stringify(initialPageErrors)}`);
    console.log(`Initial console errors (${initialConsoleErrors.length}): ${JSON.stringify(initialConsoleErrors)}`);

    // Core assertions
    const dupErrors = initialPageErrors.filter((e) => e.includes('each_key_duplicate'));
    expect(dupErrors, `each_key_duplicate on load: ${dupErrors.join('; ')}`).toHaveLength(0);
  });

  test('switch underlying twice: pageerror count during switches', async ({ page }) => {
    const pageErrors = [];
    const consoleErrors = [];

    page.on('pageerror', (err) => pageErrors.push(err.message));
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        const t = msg.text();
        if (!isNoise(t)) consoleErrors.push(t);
      }
    });

    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );
    await page.goto(PAGE_PATH, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.opt-picker', { timeout: 20000 });

    // Wait for initial strategy-analytics call to settle
    await page
      .waitForResponse((r) => r.url().includes('strategy-analytics'), { timeout: 30000 })
      .catch(() => null);

    const errsBefore = pageErrors.length;

    // Find the Underlying select — second .opt-field in the picker bar
    // (Account is first, Underlying is second)
    const optFields = page.locator('.opt-field');
    const fieldCount = await optFields.count();
    console.log(`opt-field count: ${fieldCount}`);

    // Try each opt-field to find the underlying selector
    let switched = false;
    for (let i = 0; i < fieldCount && !switched; i++) {
      const field = optFields.nth(i);
      const label = await field.locator('label').textContent().catch(() => '');
      console.log(`opt-field[${i}] label: "${label.trim()}"`);

      if (/underlying|symbol|under/i.test(label)) {
        // Found the underlying field — find its trigger
        const trigger = field.locator('button, [role="combobox"], .rq-sel-val, .sel-val').first();
        if (await trigger.isVisible().catch(() => false)) {
          const firstText = await trigger.textContent();
          console.log(`Underlying trigger text: "${firstText?.trim()}"`);

          // First switch
          await trigger.click();
          await page.waitForTimeout(500);
          const opts = page.locator('[role="option"], .rq-dropdown-item, .sel-opt');
          const optCount = await opts.count();
          console.log(`Dropdown options: ${optCount}`);

          if (optCount >= 2) {
            const secondText = await opts.nth(1).textContent().catch(() => '');
            await opts.nth(1).click();
            console.log(`Switched to: "${secondText?.trim()}"`);
            await page.waitForTimeout(2500);

            // Wait for strategy-analytics after switch
            await page
              .waitForResponse((r) => r.url().includes('strategy-analytics'), { timeout: 15000 })
              .catch(() => null);

            // Second switch — back to first
            await trigger.click();
            await page.waitForTimeout(500);
            const backOpts = page.locator('[role="option"], .rq-dropdown-item, .sel-opt');
            if (await backOpts.count() >= 1) {
              await backOpts.first().click();
              console.log('Switched back to first underlying');
              await page.waitForTimeout(2500);

              // Wait for strategy-analytics after switch back
              await page
                .waitForResponse((r) => r.url().includes('strategy-analytics'), { timeout: 15000 })
                .catch(() => null);
            } else {
              await page.keyboard.press('Escape');
            }
            switched = true;
          } else if (optCount === 1) {
            console.log('Only 1 underlying available — switch test skipped');
            await page.keyboard.press('Escape');
            switched = true;
          } else {
            console.log('No options visible — trying Escape');
            await page.keyboard.press('Escape');
          }
        }
        break;
      }
    }

    if (!switched) {
      // Fallback: try any visible button in opt-picker
      console.log('Underlying field not found by label — trying any Select trigger in .opt-picker');
      const pickerBtns = page.locator('.opt-picker button');
      const btnCount = await pickerBtns.count();
      console.log(`Buttons in .opt-picker: ${btnCount}`);
    }

    const errsAfter = pageErrors.length;
    const switchErrors = pageErrors.slice(errsBefore);
    const dupErrors = switchErrors.filter((e) => e.includes('each_key_duplicate'));
    const allConsoleErrors = [...consoleErrors];

    console.log(`Pageerrors during switches: ${switchErrors.length}`);
    console.log(`Switch pageerror detail: ${JSON.stringify(switchErrors)}`);
    console.log(`each_key_duplicate during switches: ${dupErrors.length}`);
    console.log(`Console errors during switches: ${JSON.stringify(allConsoleErrors)}`);

    expect(dupErrors, `each_key_duplicate during underlying switch: ${dupErrors.join('; ')}`).toHaveLength(0);
  });

  test('idle 30s: pageerror count + request count + console errors', async ({ page }) => {
    const pageErrors = [];
    const consoleErrors = [];
    const requests = [];

    page.on('pageerror', (err) => pageErrors.push(err.message));
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        const t = msg.text();
        if (!isNoise(t)) consoleErrors.push(t);
      }
    });
    page.on('request', (req) => requests.push({ url: req.url(), ts: Date.now() }));

    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );
    await page.goto(PAGE_PATH, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.opt-picker', { timeout: 20000 });

    // Settle: initial analytics load
    await page
      .waitForResponse((r) => r.url().includes('strategy-analytics'), { timeout: 30000 })
      .catch(() => null);
    await page.waitForTimeout(5000);

    // Start idle window
    const idleStart = Date.now();
    const errsAtIdleStart = pageErrors.length;

    await page.waitForTimeout(30000);

    const idlePageErrors = pageErrors.slice(errsAtIdleStart);
    const idleRequests = requests.filter((r) => r.ts > idleStart);
    const dupErrors = idlePageErrors.filter((e) => e.includes('each_key_duplicate'));

    console.log(`Idle 30s pageerrors: ${idlePageErrors.length}`);
    console.log(`Idle 30s each_key_duplicate: ${dupErrors.length}`);
    console.log(`Idle 30s request count: ${idleRequests.length}`);
    console.log(`Idle 30s console errors: ${JSON.stringify(consoleErrors)}`);
    console.log(`Idle pageerror detail: ${JSON.stringify(idlePageErrors)}`);

    // Final visibility check after idle
    const payoffVisible = await page.locator('.opt-payoff').first().isVisible().catch(() => false);
    const svgVisible = payoffVisible
      ? await page.locator('.payoff-svg-stack').first().isVisible().catch(() => false)
      : false;

    console.log(`.opt-payoff visible after idle: ${payoffVisible}`);
    console.log(`.payoff-svg-stack visible after idle: ${svgVisible}`);

    expect(dupErrors, `each_key_duplicate during 30s idle: ${dupErrors.join('; ')}`).toHaveLength(0);
    expect(idleRequests.length).toBeLessThan(400);
  });
});
