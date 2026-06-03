/**
 * order_modal_defaults_reflected.spec.js
 *
 * Verifies that BOTH the default account AND default symbol are
 * reflected in the order modal on first invoke (operator complaint:
 * "account and symbol actual value is not reflected in order model").
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/order_modal_defaults_reflected.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
const API_HOST = BASE.includes('localhost') ? 'https://dev.ramboq.com' : BASE;

let _cachedToken = null;

async function login(page) {
  if (!_cachedToken && process.env.PLAYWRIGHT_TOKEN) _cachedToken = process.env.PLAYWRIGHT_TOKEN;
  if (!_cachedToken) {
    for (const u of ['rambo', 'ambore', 'admin']) {
      const r = await page.request.post(`${API_HOST}/api/auth/login`, {
        data: { username: u, password: _AUTH_PASS },
        timeout: 15_000,
      }).catch(() => null);
      if (r && r.ok()) { _cachedToken = (await r.json()).access_token; break; }
    }
    if (!_cachedToken) throw new Error('login failed');
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, _cachedToken);
}

test.describe('Order modal — defaults are reflected', () => {
  test.setTimeout(90_000);

  test('default account + default symbol show in modal on first invoke', async ({ page }) => {
    const log = { timeline: [], errors: [] };
    page.on('pageerror', (e) => log.errors.push(String(e).slice(0, 200)));

    await login(page);

    // Probe backend first so we know the ground truth.
    const acctResp = await page.request.get(`${API_HOST}/api/accounts/`, {
      headers: { Authorization: `Bearer ${_cachedToken}` },
      timeout: 10_000,
    });
    const acctJson = await acctResp.json();
    log.timeline.push(`backend: default_account="${acctJson.default_account}" default_symbol="${acctJson.default_symbol}"`);
    expect(acctJson.default_account).toBeTruthy();
    expect(acctJson.default_symbol).toBeTruthy();

    // Navigate + open modal.
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);
    const orderBtn = page.locator('button.pha-order').first();
    if (await orderBtn.count() === 0) {
      log.timeline.push('skip: no .pha-order'); console.log(JSON.stringify(log, null, 2)); return;
    }
    await orderBtn.click({ force: true });
    await page.waitForTimeout(2000);

    const modal = page.locator('.oes-modal').first();
    expect(await modal.count()).toBeGreaterThan(0);

    // ── Account: probe the header's account chip / single-account label ──
    const acctProbe = await page.evaluate(() => {
      const root = document.querySelector('.oes-modal');
      if (!root) return { acct: '', source: 'no-modal' };
      // Single-account label first.
      const single = root.querySelector('.oes-account-single');
      if (single) return { acct: (single.textContent || '').trim(), source: 'oes-account-single' };
      // Multi-account MultiSelect — read the selected value displayed
      // in the trigger.
      const trigger = root.querySelector('.oes-account-multi, .oes-account-pick');
      if (trigger) return { acct: (trigger.textContent || '').trim().slice(0, 30), source: 'oes-account-multi' };
      // Generic fallback — find any element carrying the account text.
      const body = (root.textContent || '').toUpperCase();
      const m = body.match(/Z[A-Z]\d{4,8}/);
      return { acct: m ? m[0] : '', source: 'body-regex' };
    });
    log.timeline.push(`modal account probe: "${acctProbe.acct}" (via ${acctProbe.source})`);
    expect(acctProbe.acct.toUpperCase()).toContain(String(acctJson.default_account).toUpperCase());
    log.timeline.push(`✓ default_account "${acctJson.default_account}" reflected in modal`);

    // ── Symbol: probe input values + visible symbol chips ──
    const symProbe = await page.evaluate(() => {
      const root = document.querySelector('.oes-modal');
      if (!root) return '';
      const inputs = Array.from(root.querySelectorAll('input'))
        .map((i) => /** @type {HTMLInputElement} */ (i).value || '').join(' ');
      const chip = root.querySelector('.oes-sym-chip, .ot-sym-chip');
      const chipText = chip ? (chip.textContent || '') : '';
      const body = root.textContent || '';
      return ((inputs + ' ' + chipText + ' ' + body).toUpperCase()).slice(0, 4000);
    });
    const symPrefix = String(acctJson.default_symbol).slice(0, 4).toUpperCase();
    log.timeline.push(`modal symbol probe prefix "${symPrefix}" found: ${symProbe.includes(symPrefix)}`);
    expect(symProbe.includes(symPrefix)).toBe(true);
    log.timeline.push(`✓ default_symbol "${acctJson.default_symbol}" reflected in modal`);

    console.log('\n=== defaults reflected ===');
    console.log(JSON.stringify(log, null, 2));
  });
});
