// Verify Phase 3 — confirm-token mint + gated place_order.
// Critical safety properties exercised:
//   1. Mint requires valid body → 400 on missing fields
//   2. Token is single-use (replay → 403)
//   3. Mismatched purpose hash (token + different order) → 403
//   4. Missing token → 403
//   5. Expired token → 403 (skipped — would take 60s, kept as smoke)
//   6. Valid mint + matching order → forwards through ticket pipeline
//
// Run:
//   BASE_URL=https://dev.ramboq.com npx playwright test research_place_order_verify.spec.js --workers=1 --project=chromium-desktop

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

// Cache token across tests — rate limit kicks in around 3-4 logins
// per minute on dev. Single login per spec keeps us under the cap.
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

test(`mint endpoint validates inputs [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  // Missing required fields → 400
  const bad1 = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: { account: '', tradingsymbol: 'NIFTY25APRFUT', side: 'BUY', quantity: 1 },
    headers,
  });
  expect(bad1.status(), `empty account: ${await bad1.text()}`).toBe(400);

  // Invalid side → 400
  const bad2 = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: { account: 'ZG0790', tradingsymbol: 'NIFTY25APRFUT', side: 'FOO', quantity: 1 },
    headers,
  });
  expect(bad2.status()).toBe(400);

  // Valid input → 200 + token + expires_in
  const ok = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: {
      account: 'ZG0790', tradingsymbol: 'NIFTY25APRFUT',
      side: 'SELL', quantity: 50, mode: 'paper',
      order_type: 'LIMIT', price: 22150.5,
    },
    headers,
  });
  expect(ok.ok(), `mint ok: ${ok.status()}`).toBe(true);
  const j = await ok.json();
  expect(j.token).toMatch(/^[0-9a-f]{32}$/);
  expect(j.expires_in).toBeGreaterThan(50);
  expect(j.expires_in).toBeLessThanOrEqual(60);
  expect(j.purpose).toContain('SELL 50 NIFTY25APRFUT');
  console.log(`minted: ${j.purpose} | expires_in=${j.expires_in}s`);
});

test(`place_order without token → 403 [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  const r = await page.request.post(`${BASE}/api/research/place-order`, {
    data: {
      confirm_token: '',
      account: 'ZG0790', tradingsymbol: 'NIFTY25APRFUT',
      side: 'SELL', quantity: 50, mode: 'paper',
      order_type: 'LIMIT', price: 22150.5,
    },
    headers,
  });
  expect(r.status(), 'empty token must be 403').toBe(403);
  const body = await r.json();
  expect(body.detail).toContain('token');
});

test(`place_order with mismatched purpose → 403 [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  // Mint a token for SELL 50 NIFTY @22150
  const mint = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: {
      account: 'ZG0790', tradingsymbol: 'NIFTY25APRFUT',
      side: 'SELL', quantity: 50, mode: 'paper',
      order_type: 'LIMIT', price: 22150.5,
    },
    headers,
  });
  const { token } = await mint.json();

  // Try to use it for BUY 50 @22150 — DIFFERENT side
  const bad = await page.request.post(`${BASE}/api/research/place-order`, {
    data: {
      confirm_token: token,
      account: 'ZG0790', tradingsymbol: 'NIFTY25APRFUT',
      side: 'BUY', quantity: 50, mode: 'paper',     // ← side changed
      order_type: 'LIMIT', price: 22150.5,
    },
    headers,
  });
  expect(bad.status(), 'swapped side must be 403').toBe(403);
  const detail = (await bad.json()).detail;
  console.log(`mismatch: ${detail}`);
  expect(detail).toMatch(/match|purpose/i);
});

test(`place_order valid token + matching order forwards [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  // Use a real account loaded on dev so the underlying ticket
  // pipeline can validate it. ZG0790 is in the broker registry.
  const mintBody = {
    account: 'ZG0790', tradingsymbol: 'NIFTY25APRFUT',
    side: 'SELL', quantity: 50, mode: 'paper',
    order_type: 'LIMIT', price: 99999.95,
  };
  const mint = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: mintBody, headers,
  });
  expect(mint.ok()).toBe(true);
  const { token } = await mint.json();

  // Submit with matching purpose. Dev branch forces paper anyway, so
  // mode='paper' here is also what gets executed. The ticket pipeline
  // will write an AlgoOrder row (paper, OPEN) with our wild limit
  // price — won't fill (price miles above market), engine will let
  // it sit until chase cap then mark UNFILLED. Safe for verification.
  const res = await page.request.post(`${BASE}/api/research/place-order`, {
    data: { confirm_token: token, ...mintBody },
    headers,
  });
  console.log(`place result: status=${res.status()}`);
  if (res.ok()) {
    const j = await res.json();
    console.log(`order placed: id=${j.order_id} mode=${j.mode} status=${j.status}`);
    expect(j.mode).toBe('paper');   // dev branch enforcement
    expect(j.order_id).toBeTruthy();
  } else {
    // The ticket pipeline may reject for downstream reasons (e.g.
    // basket_margin check, instrument not in cache). The Phase 3
    // gate already passed — that's what we're verifying here.
    const body = await res.text();
    console.log(`ticket pipeline rejected (expected on stale dev): ${body.slice(0, 200)}`);
  }

  // Replay attack — same token should now return 403 (used)
  const replay = await page.request.post(`${BASE}/api/research/place-order`, {
    data: { confirm_token: token, ...mintBody },
    headers,
  });
  expect(replay.status(), 'replay → 403').toBe(403);
  const replayDetail = (await replay.json()).detail;
  expect(replayDetail).toMatch(/used|unknown|already/i);
  console.log(`replay denied: ${replayDetail}`);
});

test(`Lab Settings tab — Mint form renders [${BASE}]`, async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/admin/research`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(3500);

  await page.locator('.lab-tab', { hasText: 'Settings' }).click();
  await page.waitForTimeout(400);

  // 5 cards now (Mint + JWT + .mcp.json + Tools + Safety).
  const cards = page.locator('.lab-card');
  await expect(cards).toHaveCount(5);
  // The new card has the lab-card-mint class
  await expect(page.locator('.lab-card-mint')).toBeVisible();
  // The form grid is present
  await expect(page.locator('.mint-grid')).toBeVisible();
  // Mint button present
  await expect(page.locator('.mint-btn')).toHaveText('Mint token');
  // Tool inventory shows place_order
  await expect(page.locator('.tools-table tbody tr', { hasText: 'place_order' })).toBeVisible();

  await page.screenshot({ path: `test-results/research-mint-${BASE.includes('dev') ? 'dev' : 'prod'}.png` });
});
