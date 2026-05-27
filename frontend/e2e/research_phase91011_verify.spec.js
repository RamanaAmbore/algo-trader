// Verify Phase 9 + 10 + 11.
//   - Tools table on Settings tab now has 21 entries
//   - 3 new tool names visible: get_watchlist / get_pnl_attribution /
//     get_funds_summary
//   - Smoke-test the underlying endpoints respond at the expected
//     status codes (auth gate + happy-path shape)

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

test(`underlying endpoints respond [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}` };

  // GET /api/funds/ (used by get_funds_summary)
  const funds = await page.request.get(`${BASE}/api/funds/`, { headers });
  expect(funds.ok(), `funds status: ${funds.status()}`).toBe(true);
  const fundsJ = await funds.json();
  expect(Array.isArray(fundsJ.rows)).toBe(true);
  console.log(`funds rows: ${fundsJ.rows.length}`);

  // GET /api/watchlist/ (used by get_watchlist)
  const wl = await page.request.get(`${BASE}/api/watchlist/`, { headers });
  expect(wl.ok(), `watchlist status: ${wl.status()}`).toBe(true);
  const wlJ = await wl.json();
  expect(Array.isArray(wlJ)).toBe(true);
  console.log(`watchlists: ${wlJ.length} (names: ${wlJ.map(w => w.name).slice(0, 5).join(', ')})`);

  // GET /api/admin/pnl/by-agent (used by get_pnl_attribution)
  const pnl = await page.request.get(
    `${BASE}/api/admin/pnl/by-agent?period=today&mode=all`, { headers });
  expect(pnl.ok(), `pnl status: ${pnl.status()}`).toBe(true);
  const pnlJ = await pnl.json();
  expect(Array.isArray(pnlJ)).toBe(true);
  console.log(`pnl agents: ${pnlJ.length}`);
});

test(`Settings tab — 21 tools incl 3 new entries [${BASE}]`, async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/admin/research`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(3500);
  await page.locator('.lab-tab', { hasText: 'Settings' }).click();
  await page.waitForTimeout(400);
  const toolRows = page.locator('.tools-table tbody tr');
  await expect(toolRows).toHaveCount(21);
  await expect(page.locator('.tools-table tbody tr', { hasText: 'get_watchlist' })).toBeVisible();
  await expect(page.locator('.tools-table tbody tr', { hasText: 'get_pnl_attribution' })).toBeVisible();
  await expect(page.locator('.tools-table tbody tr', { hasText: 'get_funds_summary' })).toBeVisible();
});
