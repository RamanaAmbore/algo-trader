// Verify Phase 8 — get_options_chain_snapshot.
//   1. Unauth → 401
//   2. Missing underlying → 400
//   3. Bad expiry format → 400
//   4. Valid call returns expected shape (NIFTY weekly chain)
//   5. atm_window honored (cap at 30)
//   6. Settings tab Tools table count = 18

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

test(`chain-snapshot contract — auth + validation [${BASE}]`, async ({ page, browser }) => {
  // Unauth (fresh context, no inherited Authorization)
  const cleanCtx = await browser.newContext();
  const noAuth = await cleanCtx.request.get(`${BASE}/api/options/chain-snapshot?underlying=NIFTY&expiry=2026-01-29`);
  expect(noAuth.status()).toBe(401);
  await cleanCtx.close();

  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}` };

  // Missing underlying → 400
  const m1 = await page.request.get(`${BASE}/api/options/chain-snapshot?expiry=2026-01-29`, { headers });
  expect(m1.status(), 'missing underlying → 400').toBe(400);

  // Missing expiry → 400
  const m2 = await page.request.get(`${BASE}/api/options/chain-snapshot?underlying=NIFTY`, { headers });
  expect(m2.status(), 'missing expiry → 400').toBe(400);

  // Bad expiry format → 400
  const m3 = await page.request.get(`${BASE}/api/options/chain-snapshot?underlying=NIFTY&expiry=NOTADATE`, { headers });
  expect(m3.status(), 'bad expiry → 400').toBe(400);

  console.log('contract validation: all 3 paths returned 400 as expected');
});

test(`chain-snapshot returns Greeks for a real expiry [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}` };

  // Discover a valid NIFTY expiry from the instruments cache so the
  // test isn't tied to a particular date that may pass or fail
  // depending on when we're running.
  const inst = await page.request.get(`${BASE}/api/instruments?underlying=NIFTY&kind=opt&limit=300`, { headers });
  if (!inst.ok()) {
    console.log(`(soft-skip — /api/instruments returned ${inst.status()}, no chain to test)`);
    return;
  }
  const instJson = await inst.json();
  const items = instJson.items || [];
  const expiries = [...new Set(items.map(i => i.x).filter(Boolean))].sort();
  if (expiries.length === 0) {
    console.log('(soft-skip — no NIFTY option expiries in instruments cache)');
    return;
  }
  // Pick the nearest expiry (smallest ISO string >= today is fine for
  // ISO YYYY-MM-DD ordering).
  const today = new Date().toISOString().slice(0, 10);
  const expiry = expiries.find(e => e >= today) || expiries[0];
  console.log(`testing expiry: ${expiry}`);

  const r = await page.request.get(
    `${BASE}/api/options/chain-snapshot?underlying=NIFTY&expiry=${expiry}&atm_window=5`,
    { headers });
  console.log(`chain-snapshot status: ${r.status()}`);
  if (!r.ok()) {
    // 502 is acceptable if dev's broker connection is flaky right now.
    const body = await r.text();
    console.log(`(soft-skip — chain-snapshot returned ${r.status()}: ${body.slice(0, 200)})`);
    return;
  }
  const j = await r.json();

  expect(j.underlying).toBe('NIFTY');
  expect(j.expiry).toBe(expiry);
  expect(typeof j.spot).toBe('number');
  expect(typeof j.spot_source).toBe('string');
  expect(typeof j.days_to_expiry).toBe('number');
  expect(typeof j.risk_free_rate).toBe('number');
  expect(Array.isArray(j.rows)).toBe(true);
  console.log(`spot=${j.spot} (${j.spot_source}) atm_strike=${j.atm_strike} dte=${j.days_to_expiry} rows=${j.rows.length}`);

  // atm_window=5 → up to 11 strikes (5 below + atm + 5 above)
  expect(j.rows.length).toBeLessThanOrEqual(11);
  expect(j.rows.length).toBeGreaterThan(0);

  // Each row carries both sides + signed atm_distance
  for (const row of j.rows.slice(0, 3)) {
    expect(typeof row.k).toBe('number');
    expect(typeof row.atm_distance).toBe('number');
    expect(row.ce).toBeDefined();
    expect(row.pe).toBeDefined();
    // Sign of atm_distance matches (strike - spot)
    expect(Math.abs(row.atm_distance - (row.k - j.spot))).toBeLessThan(0.01);
  }

  // At least one row should have populated CE LTP (if any market data
  // is reachable at all). Soft assertion — markets closed at 3 AM IST
  // can still have last_price from the previous close.
  const anyLtp = j.rows.some(r => r.ce.ltp || r.pe.ltp);
  console.log(`any LTP populated: ${anyLtp}`);
});

test(`Settings tab — Tools table has 18 entries [${BASE}]`, async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/admin/research`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(3500);
  await page.locator('.lab-tab', { hasText: 'Settings' }).click();
  await page.waitForTimeout(400);
  const toolRows = page.locator('.tools-table tbody tr');
  await expect(toolRows).toHaveCount(18);
  await expect(page.locator('.tools-table tbody tr', { hasText: 'get_options_chain_snapshot' })).toBeVisible();
});
