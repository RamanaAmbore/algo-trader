/**
 * Quick probe to understand the derivatives page DOM after strategy-analytics 201
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ baseURL: 'https://dev.ramboq.com' });
test.setTimeout(120_000);

test('probe derivatives page DOM', async ({ page }) => {
  const pageErrors = [];
  const consoleAll = [];

  page.on('pageerror', (err) => pageErrors.push(err.message));
  page.on('console', (msg) => {
    consoleAll.push({ type: msg.type(), text: msg.text().slice(0, 200) });
  });

  await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
    loginAsAdmin(page, { user: 'rambo' })
  );

  await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });

  // Wait for strategy-analytics
  const stratResp = await page
    .waitForResponse((r) => r.url().includes('strategy-analytics'), { timeout: 45000 })
    .catch(() => null);

  // Extra wait for reactive update
  await page.waitForTimeout(3000);

  const url = page.url();
  console.log(`Final URL: ${url}`);

  // Check what classes exist
  const optPayoff = await page.locator('.opt-payoff').count();
  const payoffSvg = await page.locator('.payoff-svg-stack').count();
  const optPicker = await page.locator('.opt-picker').count();
  const candScroll = await page.locator('.cand-scroll').count();
  const optField = await page.locator('.opt-field').count();

  console.log(`.opt-payoff count: ${optPayoff}`);
  console.log(`.payoff-svg-stack count: ${payoffSvg}`);
  console.log(`.opt-picker count: ${optPicker}`);
  console.log(`.cand-scroll count: ${candScroll}`);
  console.log(`.opt-field count: ${optField}`);

  // Dump the page body class names to understand layout
  const bodyClasses = await page.evaluate(() => {
    const els = document.querySelectorAll('[class]');
    const clsSet = new Set();
    els.forEach(el => {
      el.className.split(' ').forEach(c => { if (c.startsWith('opt-') || c.startsWith('payoff') || c.startsWith('cand')) clsSet.add(c); });
    });
    return [...clsSet].sort();
  });
  console.log(`Relevant classes on page: ${JSON.stringify(bodyClasses)}`);

  // Strategy-analytics response body
  if (stratResp) {
    const body = await stratResp.json().catch(() => null);
    console.log(`strategy-analytics status: ${stratResp.status()}`);
    if (body) {
      console.log(`strategy-analytics body keys: ${Object.keys(body).join(', ')}`);
      console.log(`strategy-analytics legs: ${body.legs?.length ?? 'n/a'}`);
    }
  }

  // Check candidates panel
  const candRows = await page.locator('.cand-row, .cand-body tr, [class*="cand-row"]').count();
  console.log(`Candidate rows: ${candRows}`);

  // Check if any legs are checked
  const checkedLegs = await page.locator('input[type="checkbox"]:checked').count();
  console.log(`Checked leg checkboxes: ${checkedLegs}`);

  // Page errors summary
  console.log(`Pageerrors: ${JSON.stringify(pageErrors)}`);
  const errors = consoleAll.filter(m => m.type === 'error');
  console.log(`Console errors: ${JSON.stringify(errors)}`);

  // Screenshot for visual reference
  await page.screenshot({ path: '/tmp/derivatives_iter2_probe.png', fullPage: false });
  console.log('Screenshot saved to /tmp/derivatives_iter2_probe.png');
});
