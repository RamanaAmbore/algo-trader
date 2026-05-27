// Verify Phase 2c — GET /api/economic/snapshot returns all five
// macros with the expected freshness fields, and unauthorized
// requests get 401 (admin-guarded).

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

async function login(page) {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  if (!tok) throw new Error(`login failed against ${BASE}`);
  await page.context().addInitScript((t) => { sessionStorage.setItem('ramboq_token', t); }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });
  return tok;
}

test(`economic snapshot endpoint contract [${BASE}]`, async ({ page }) => {
  // Unauth → 401
  const noAuth = await page.request.get(`${BASE}/api/economic/snapshot`);
  expect(noAuth.status(), 'unauth must be 401').toBe(401);

  // Auth → 200 with all five fields present.
  const tok = await login(page);
  const r = await page.request.get(`${BASE}/api/economic/snapshot`, {
    headers: { Authorization: `Bearer ${tok}` },
  });
  expect(r.ok(), `auth get: ${r.status()}`).toBe(true);
  const j = await r.json();

  for (const key of ['repo_rate', 'cpi', 'iip', 'gdp_growth', 'inr_usd']) {
    expect(key in j, `missing field: ${key}`).toBe(true);
    if (j[key]) {
      const m = j[key];
      expect(typeof m.value, `${key}.value type`).toBe('number');
      expect(typeof m.as_of, `${key}.as_of type`).toBe('string');
      expect(typeof m.age_days, `${key}.age_days type`).toBe('number');
      expect(typeof m.stale, `${key}.stale type`).toBe('boolean');
      expect(typeof m.label, `${key}.label type`).toBe('string');
      console.log(`  ${key}: ${m.value}  as_of=${m.as_of}  age=${m.age_days}d  stale=${m.stale}  (${m.label})`);
    }
  }
  expect(typeof j.refreshed_at).toBe('string');
});

test(`research page Settings tab shows new tool [${BASE}]`, async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/admin/research`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(3500);
  await page.locator('.lab-tab', { hasText: 'Settings' }).click();
  await page.waitForTimeout(400);

  // Tools table now has 13 rows including get_economic_snapshot.
  const rows = page.locator('.tools-table tbody tr');
  await expect(rows).toHaveCount(13);
  const econRow = page.locator('.tools-table tbody tr', { hasText: 'get_economic_snapshot' });
  await expect(econRow).toBeVisible();
});
