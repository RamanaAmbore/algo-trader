/**
 * Probe 2 — capture the boundary console.warn to see what error it's swallowing
 */
import { test } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ baseURL: 'https://dev.ramboq.com' });
test.setTimeout(120_000);

test('probe derivatives boundary error message', async ({ page }) => {
  const pageErrors = [];
  const consoleWarns = [];
  const consoleErrors = [];

  page.on('pageerror', (err) => pageErrors.push(err.message));
  page.on('console', (msg) => {
    const text = msg.text();
    if (msg.type() === 'warn') consoleWarns.push(text);
    if (msg.type() === 'error') consoleErrors.push(text);
    // Also log any derivatives boundary message
    if (text.includes('derivatives') || text.includes('boundary') || text.includes('each_key') || text.includes('ReferenceError') || text.includes('TypeError') || text.includes('undefined')) {
      console.log(`[${msg.type().toUpperCase()}] ${text.slice(0, 300)}`);
    }
  });

  await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
    loginAsAdmin(page, { user: 'rambo' })
  );

  await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(8000);

  console.log(`--- ALL pageerrors (${pageErrors.length}) ---`);
  pageErrors.forEach(e => console.log(` PG: ${e.slice(0, 300)}`));

  console.log(`--- ALL warns (${consoleWarns.length}) ---`);
  consoleWarns.forEach(w => console.log(` WN: ${w.slice(0, 300)}`));

  console.log(`--- ALL console errors (${consoleErrors.length}) ---`);
  consoleErrors.forEach(e => console.log(` CE: ${e.slice(0, 300)}`));

  // Check the DOM — is there ANYTHING rendered in the main page area?
  const bodyHTML = await page.evaluate(() => document.body.innerHTML.length);
  const mainChildren = await page.evaluate(() => document.querySelector('main')?.children.length ?? -1);
  const allDivs = await page.evaluate(() => document.querySelectorAll('div').length);

  console.log(`Body HTML length: ${bodyHTML}`);
  console.log(`Main children: ${mainChildren}`);
  console.log(`Total divs: ${allDivs}`);

  // Try to grab any text content on the page
  const pageText = await page.evaluate(() => document.body.innerText.slice(0, 500));
  console.log(`Page text (first 500): ${pageText}`);
});
