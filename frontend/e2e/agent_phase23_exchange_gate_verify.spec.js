// Verify Phase 23 — per-order exchange-open gate on /api/orders/ticket.
// At any wall-clock time when EITHER exchange is closed, the gate must
// return 409 with the segment name in the detail.

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
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
  return _cachedToken;
}

test(`exchange-open gate behaviour vs wall-clock IST [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  // Compute current IST hour to decide expected behaviour
  const istNow = new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata',
                                                      hour: 'numeric', hour12: false });
  const istHour = parseInt(istNow, 10);
  // NSE equity: 09:15-15:30 IST. MCX: 09:00-23:30 IST. Both closed
  // late evening (after 23:30) and overnight (00:00-09:00).
  const nseOpen = istHour >= 9 && istHour < 16;
  const mcxOpen = istHour >= 9 && istHour < 23;
  console.log(`IST hour: ${istHour} — NSE open: ${nseOpen}, MCX open: ${mcxOpen}`);

  // NFO order — gates against nse_open. v2 API (2026-07-08): qty is LOTS.
  const nfoRes = await page.request.post(`${BASE}/api/orders/ticket`, {
    data: {
      mode: 'paper', side: 'SELL', tradingsymbol: 'NIFTY25APRFUT',
      quantity: 1, exchange: 'NFO', product: 'NRML',
      order_type: 'LIMIT', price: 99999.95, account: 'ZG0790',
    }, headers,
  });
  const nfoStatus = nfoRes.status();
  const nfoBody = await nfoRes.json();
  console.log(`NFO request → HTTP ${nfoStatus}: ${nfoBody.detail || nfoBody.order_id}`);
  if (nseOpen) {
    // Gate should pass — expect 201 (order accepted) or 400/500 from
    // the ticket pipeline itself (basket margin reject, etc.). Just
    // assert NOT 409.
    expect(nfoStatus, 'NFO during NSE hours should not be gate-blocked').not.toBe(409);
  } else {
    expect(nfoStatus, 'NFO outside NSE hours should be 409').toBe(409);
    expect(nfoBody.detail).toMatch(/Exchange NFO is closed/);
  }

  // MCX order — v2 API: qty is LOTS
  const mcxRes = await page.request.post(`${BASE}/api/orders/ticket`, {
    data: {
      mode: 'paper', side: 'SELL', tradingsymbol: 'CRUDEOILM25MAY5500CE',
      quantity: 1, exchange: 'MCX', product: 'NRML',
      order_type: 'LIMIT', price: 50.0, account: 'ZG0790',
    }, headers,
  });
  const mcxStatus = mcxRes.status();
  const mcxBody = await mcxRes.json();
  console.log(`MCX request → HTTP ${mcxStatus}: ${mcxBody.detail || mcxBody.order_id}`);
  if (mcxOpen) {
    expect(mcxStatus, 'MCX during MCX hours should not be gate-blocked').not.toBe(409);
  } else {
    expect(mcxStatus, 'MCX outside MCX hours should be 409').toBe(409);
    expect(mcxBody.detail).toMatch(/Exchange MCX is closed/);
  }
});

test(`unknown exchange code is rejected [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  // Made-up exchange — should fail at the existing _EXCHANGES enum check
  // (400) before hitting the new gate.
  const r = await page.request.post(`${BASE}/api/orders/ticket`, {
    data: {
      mode: 'paper', side: 'SELL', tradingsymbol: 'X',
      quantity: 1, exchange: 'FAKE', account: 'ZG0790',
    }, headers,
  });
  // Either 400 (enum validation) or 409 (gate). Both are "blocked" —
  // operator gets a clear reason.
  expect([400, 409, 422]).toContain(r.status());
});
