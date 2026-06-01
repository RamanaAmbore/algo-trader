/**
 * probe.spec.js — minimal auth + page-state probe.
 * Confirm whether seeding sessionStorage actually authenticates us.
 */
import { test } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
const API_HOST = BASE.includes('localhost') ? 'https://dev.ramboq.com' : BASE;

let _cachedToken = null;

async function login(page) {
  if (!_cachedToken) {
    for (const u of ['ambore', 'rambo', 'admin']) {
      const r = await page.request.post(`${API_HOST}/api/auth/login`, {
        data: { username: u, password: _AUTH_PASS },
        timeout: 15_000,
      }).catch(() => null);
      if (r && r.ok()) { _cachedToken = (await r.json()).access_token; console.log(`logged in as ${u}`); break; }
    }
    if (!_cachedToken) throw new Error('login failed');
  }
}

test('probe: dump page state for /orders and /pulse', async ({ page }) => {
  test.setTimeout(90_000);
  await login(page);

  // Try several common token storage keys
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
    localStorage.setItem('ramboq_token', t);
    sessionStorage.setItem('token', t);
    localStorage.setItem('token', t);
    sessionStorage.setItem('access_token', t);
    localStorage.setItem('access_token', t);
  }, _cachedToken);

  for (const path of ['/orders', '/pulse', '/dashboard']) {
    console.log(`\n=== ${path} ===`);
    await page.goto(`${BASE}${path}`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(4000);
    const url = page.url();
    const title = await page.title();
    const navText = await page.locator('nav').first().innerText().catch(() => '<no nav>');
    const bodyTextHead = (await page.locator('body').innerText().catch(() => '')).slice(0, 400);
    const tokenInSS = await page.evaluate(() => sessionStorage.getItem('ramboq_token'));
    console.log(`  url=${url}`);
    console.log(`  title=${title}`);
    console.log(`  sessionStorage.ramboq_token present: ${!!tokenInSS}`);
    console.log(`  nav text: ${navText.slice(0, 200).replace(/\n/g, ' | ')}`);
    console.log(`  body[0..400]: ${bodyTextHead.replace(/\n/g, ' | ')}`);
  }
});
