// Verify Phase 5 — paper-aware cancel + modify.
//   1. mode='paper' cancel + modify mint paths succeed
//   2. Cross-mode rejection: live-cancel token can't be redeemed for paper cancel
//   3. paper cancel of non-existent AlgoOrder.id returns 404 (not 500)
//   4. paper cancel with non-integer order_id returns 400
//   5. Frontend Mode dropdown appears for CANCEL + MODIFY kinds

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

let _cachedToken = null;
async function login(page) {
  if (!_cachedToken) {
    for (const u of ['ambore', 'rambo']) {
      const r = await page.request.post(`${BASE}/api/auth/login`, {
        data: { username: u, password: _AUTH_PASS },
      });
      if (r.ok()) { _cachedToken = (await r.json()).access_token; break; }
    }
    if (!_cachedToken) throw new Error(`login failed against ${BASE}`);
  }
  await page.context().addInitScript((t) => { sessionStorage.setItem('ramboq_token', t); }, _cachedToken);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
  return _cachedToken;
}

test(`paper cancel + modify mints work [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  // Paper cancel
  const mintC = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: { kind: 'cancel', account: 'ZG0790', order_id: '42', mode: 'paper' },
    headers,
  });
  expect(mintC.ok(), `paper cancel mint: ${mintC.status()}`).toBe(true);
  const c = await mintC.json();
  expect(c.purpose).toContain('CANCEL [PAPER]');
  expect(c.purpose).toContain('order_id=42');
  console.log(`paper cancel mint: ${c.purpose}`);

  // Paper modify
  const mintM = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: {
      kind: 'modify', account: 'ZG0790', order_id: '42', mode: 'paper',
      quantity: 50, order_type: 'LIMIT', price: 22100,
    }, headers,
  });
  expect(mintM.ok(), `paper modify mint: ${mintM.status()}`).toBe(true);
  const m = await mintM.json();
  expect(m.purpose).toContain('MODIFY [PAPER]');
  console.log(`paper modify mint: ${m.purpose}`);
});

test(`cross-mode token redemption blocked [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  // Mint a LIVE cancel token
  const mint = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: { kind: 'cancel', account: 'ZG0790', order_id: '251115000123456', mode: 'live' },
    headers,
  });
  expect(mint.ok()).toBe(true);
  const { token } = await mint.json();

  // Try to use it for a PAPER cancel → 403
  const bad = await page.request.post(`${BASE}/api/research/cancel-order`, {
    data: {
      confirm_token: token,
      account: 'ZG0790', order_id: '251115000123456', mode: 'paper',
    }, headers,
  });
  expect(bad.status(), `cross-mode must be 403: ${bad.status()}`).toBe(403);
  console.log(`cross-mode denied: ${(await bad.json()).detail}`);
});

test(`paper cancel rejects non-integer order_id [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  // Mint matching paper-cancel token
  const mint = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: { kind: 'cancel', account: 'ZG0790', order_id: 'not-an-int', mode: 'paper' },
    headers,
  });
  expect(mint.ok()).toBe(true);
  const { token } = await mint.json();

  const bad = await page.request.post(`${BASE}/api/research/cancel-order`, {
    data: {
      confirm_token: token,
      account: 'ZG0790', order_id: 'not-an-int', mode: 'paper',
    }, headers,
  });
  expect(bad.status(), `non-integer order_id must be 400: ${bad.status()}`).toBe(400);
  console.log(`non-int denied: ${(await bad.json()).detail}`);
});

test(`paper cancel of non-existent order_id returns 404 [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  const mint = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: { kind: 'cancel', account: 'ZG0790', order_id: '99999999', mode: 'paper' },
    headers,
  });
  const { token } = await mint.json();

  const r = await page.request.post(`${BASE}/api/research/cancel-order`, {
    data: {
      confirm_token: token,
      account: 'ZG0790', order_id: '99999999', mode: 'paper',
    }, headers,
  });
  expect(r.status(), `non-existent paper id must be 404: ${r.status()}`).toBe(404);
  console.log(`non-existent paper id: ${(await r.json()).detail}`);
});

test(`Lab mint widget — Mode dropdown for cancel/modify [${BASE}]`, async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/admin/research`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(3500);

  await page.locator('.lab-tab', { hasText: 'Settings' }).click();
  await page.waitForTimeout(400);

  // Switch the Kind dropdown to CANCEL via the custom Select component
  // (open trigger → click matching option). page-internal-Select renders
  // .rbq-select-trigger + .rbq-select-option items.
  const kindTrigger = page.locator('.mint-grid .rbq-select-trigger').first();
  await kindTrigger.click();
  await page.waitForTimeout(150);
  await page.locator('.rbq-select-option', { hasText: /^CANCEL$/ }).first().click();
  await page.waitForTimeout(150);

  // Find a Mode dropdown — should have PAPER + LIVE labels
  const modeText = await page.locator('.mint-grid').innerText();
  expect(modeText).toContain('PAPER');
  expect(modeText).toContain('LIVE');

  // Order ID placeholder should hint live format by default
  const allInputPlaceholders = await page.locator('.mint-grid input').evaluateAll(els => els.map(e => e.placeholder || ''));
  console.log('cancel placeholders:', allInputPlaceholders);
  expect(allInputPlaceholders.join(' ')).toMatch(/251115|AlgoOrder/);

  // Switch to MODIFY — Mode dropdown should still be there
  await kindTrigger.click();
  await page.waitForTimeout(150);
  await page.locator('.rbq-select-option', { hasText: /^MODIFY$/ }).first().click();
  await page.waitForTimeout(150);
  const modifyText = await page.locator('.mint-grid').innerText();
  expect(modifyText).toContain('PAPER');
  expect(modifyText).toContain('LIVE');
  expect(modifyText).toContain('New qty');
  expect(modifyText).toContain('New price');

  await page.screenshot({ path: `test-results/research-mint-modify-${BASE.includes('dev') ? 'dev' : 'prod'}.png` });
});
