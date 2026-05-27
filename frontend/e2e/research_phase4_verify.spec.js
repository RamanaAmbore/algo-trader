// Verify Phase 3b/3c/4 — audit endpoint contract, cross-kind token
// rejection, and the new mint widget UI. Telegram ping itself
// can't be verified externally without parsing the bot's chat —
// success path log assertions are out of scope.

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

test(`GET /api/research/audit endpoint contract [${BASE}]`, async ({ page, browser }) => {
  // Unauth probe needs a fresh context — page.request inherits
  // setExtraHTTPHeaders if we call login() in the same test.
  const cleanCtx = await browser.newContext();
  const noAuth = await cleanCtx.request.get(`${BASE}/api/research/audit`);
  expect(noAuth.status()).toBe(401);
  await cleanCtx.close();

  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}` };

  // Auth → 200 + array
  const r = await page.request.get(`${BASE}/api/research/audit?limit=50`, { headers });
  expect(r.ok()).toBe(true);
  const rows = await r.json();
  expect(Array.isArray(rows)).toBe(true);
  // Each row carries the required shape (when any exist)
  for (const row of rows.slice(0, 3)) {
    expect(typeof row.id).toBe('number');
    expect(typeof row.tool).toBe('string');
    expect(typeof row.result_status).toBe('string');
    expect(typeof row.created_at).toBe('string');
    expect(['ok', 'denied', 'error'].includes(row.result_status)).toBe(true);
  }
  console.log(`audit rows visible: ${rows.length}`);
});

test(`cross-kind token rejection [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  // Mint a PLACE token, try to use it for CANCEL → must be 403.
  const mint = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: {
      kind: 'place',
      account: 'ZG0790', tradingsymbol: 'NIFTY25APRFUT',
      side: 'SELL', quantity: 50, mode: 'paper',
      order_type: 'LIMIT', price: 99999.95,
    }, headers,
  });
  expect(mint.ok()).toBe(true);
  const { token } = await mint.json();

  // Use the place token for a cancel → 403, purpose mismatch.
  const cancel = await page.request.post(`${BASE}/api/research/cancel-order`, {
    data: {
      confirm_token: token,
      account: 'ZG0790',
      order_id: '251115000123456',
    }, headers,
  });
  expect(cancel.status(), `place→cancel must be 403: got ${cancel.status()}`).toBe(403);
  console.log(`cross-kind denied: ${(await cancel.json()).detail}`);
});

test(`cancel + modify mint paths work [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  // CANCEL mint
  const mintC = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: { kind: 'cancel', account: 'ZG0790', order_id: '251115000123456' },
    headers,
  });
  expect(mintC.ok(), `cancel mint: ${mintC.status()}`).toBe(true);
  const c = await mintC.json();
  expect(c.purpose).toContain('CANCEL');
  expect(c.purpose).toContain('251115000123456');
  console.log(`cancel mint: ${c.purpose}`);

  // MODIFY mint
  const mintM = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: {
      kind: 'modify', account: 'ZG0790', order_id: '251115000123456',
      quantity: 25, order_type: 'LIMIT', price: 22150,
    }, headers,
  });
  expect(mintM.ok(), `modify mint: ${mintM.status()}`).toBe(true);
  const m = await mintM.json();
  expect(m.purpose).toContain('MODIFY');
  expect(m.purpose).toContain('qty=25');
  console.log(`modify mint: ${m.purpose}`);

  // Cancel mint requires order_id — empty → 400
  const bad = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: { kind: 'cancel', account: 'ZG0790', order_id: '' },
    headers,
  });
  expect(bad.status()).toBe(400);
});

test(`Lab page — Audit tab + Kind selector render [${BASE}]`, async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/admin/research`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(3500);

  // 4 tabs now: Research, Drafts, Audit, Settings
  const tabs = page.locator('.lab-tab');
  await expect(tabs).toHaveCount(4);
  const tabLabels = (await tabs.allTextContents()).map(t => t.trim());
  expect(tabLabels.join(' ')).toMatch(/Audit/);
  console.log('tabs:', tabLabels);

  // Audit tab
  await page.locator('.lab-tab', { hasText: 'Audit' }).click();
  await page.waitForTimeout(600);
  await expect(page.locator('.lab-audit')).toBeVisible();
  await expect(page.locator('.audit-filters')).toBeVisible();
  // Tool filter has all three actions
  const filterOptions = await page.locator('.audit-filters select').first().locator('option').allTextContents();
  expect(filterOptions).toContain('place_order');
  expect(filterOptions).toContain('cancel_order');
  expect(filterOptions).toContain('modify_order');

  await page.screenshot({ path: `test-results/research-audit-${BASE.includes('dev') ? 'dev' : 'prod'}.png` });

  // Settings tab — Kind selector
  await page.locator('.lab-tab', { hasText: 'Settings' }).click();
  await page.waitForTimeout(400);
  // First field is the Kind dropdown
  const kindOptions = await page.locator('.mint-grid select').first().locator('option').allTextContents();
  expect(kindOptions.length).toBe(3);
  expect(kindOptions.join(' ')).toMatch(/PLACE/);
  expect(kindOptions.join(' ')).toMatch(/CANCEL/);
  expect(kindOptions.join(' ')).toMatch(/MODIFY/);
  // Tools table now has 16 rows
  const toolRows = page.locator('.tools-table tbody tr');
  await expect(toolRows).toHaveCount(16);
  await expect(page.locator('.tools-table tbody tr', { hasText: 'cancel_order' })).toBeVisible();
  await expect(page.locator('.tools-table tbody tr', { hasText: 'modify_order' })).toBeVisible();
});
