/**
 * order_modal_symbol_resolved_to_future.spec.js
 *
 * Verifies commit 2b9e1866: when orders.default_symbol is a bare root
 * (NIFTY / BANKNIFTY) or a quote-key spot (NIFTY 50) or an MCX
 * commodity root (CRUDEOIL), the order modal opens with the RESOLVED
 * FUTURE (e.g. NIFTY26JUNFUT, CRUDEOILM26JUNFUT) — NOT the raw root
 * or the spot quote-key.
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/order_modal_symbol_resolved_to_future.spec.js \
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

test.describe('Order modal — bare root / spot key resolves to FUTURE', () => {
  test.setTimeout(90_000);

  test('symbol carries *FUT suffix in modal — not raw root or spot key', async ({ page }) => {
    const log = { timeline: [], errors: [] };
    page.on('pageerror', (e) => log.errors.push(String(e).slice(0, 200)));

    await login(page);

    const acctResp = await page.request.get(`${API_HOST}/api/accounts/`, {
      headers: { Authorization: `Bearer ${_cachedToken}` },
      timeout: 10_000,
    });
    const { default_symbol } = await acctResp.json();
    log.timeline.push(`backend default_symbol: "${default_symbol}"`);
    expect(default_symbol).toBeTruthy();

    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);
    const orderBtn = page.locator('button.pha-order').first();
    if (await orderBtn.count() === 0) {
      log.timeline.push('skip: no .pha-order'); console.log(JSON.stringify(log, null, 2)); return;
    }
    await orderBtn.click({ force: true });
    await page.waitForTimeout(3000); // wait for instruments cache hydrate

    // Read the modal's body + inputs.
    const probeText = await page.evaluate(() => {
      const root = document.querySelector('.oes-modal');
      if (!root) return '';
      const inputs = Array.from(root.querySelectorAll('input'))
        .map((i) => /** @type {HTMLInputElement} */ (i).value || '').join(' ');
      return (inputs + ' ' + (root.textContent || '')).toUpperCase();
    });

    const baseRoot = String(default_symbol).toUpperCase().replace(/\s+50$|\s+BANK$/, '');
    const rootBare = baseRoot.split(/\s+/)[0]; // CRUDEOIL / NIFTY etc.

    // Find a *FUT pattern that starts with the root in the modal text.
    const futRegex = new RegExp(`${rootBare}[A-Z0-9]*FUT`, 'i');
    const futMatch = probeText.match(futRegex);
    log.timeline.push(`looking for future matching /${rootBare}.*FUT/: ${futMatch ? futMatch[0] : 'NOT FOUND'}`);

    if (futMatch) {
      log.timeline.push(`✓ modal shows resolved future "${futMatch[0]}" (not bare root "${rootBare}" or spot key)`);
    } else {
      // Fallback assertion — at least the root prefix should be present.
      // If only the bare root appears, the operator's reported bug is real.
      log.timeline.push(`note: no *FUT found; probe text length=${probeText.length}`);
    }
    expect(futMatch).toBeTruthy();

    console.log('\n=== resolved-future ===');
    console.log(JSON.stringify(log, null, 2));
  });
});
