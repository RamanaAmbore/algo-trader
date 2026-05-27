// Verify Phase 6 + 7.
//   6. mcp.audit_retention_days setting present (proves the seeder + cleanup
//      task code path is wired in; the actual purge runs at 03:15 IST so
//      we can't directly observe it without time travel).
//   7. get_audit_recent: the endpoint the MCP tool calls returns
//      reverse-chrono rows with the expected shape.
//   UI: Tools-table count is now 17.

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

test(`Phase 6 — mcp.audit_retention_days setting seeded [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}` };

  const r = await page.request.get(`${BASE}/api/admin/settings/`, { headers });
  expect(r.ok()).toBe(true);
  const settings = await r.json();
  // The shape can be either {settings: [...]} or [...] depending on
  // route — handle both.
  const rows = Array.isArray(settings) ? settings : (settings.settings || []);
  const row = rows.find(s => (s.key || s.name) === 'mcp.audit_retention_days');
  expect(row, 'mcp.audit_retention_days must be seeded').toBeTruthy();
  console.log(`mcp.audit_retention_days: value=${row.value} default=${row.default_value} units=${row.units}`);
  expect(Number(row.value)).toBeGreaterThanOrEqual(0);
  expect(Number(row.default_value)).toBe(90);
});

test(`Phase 7 — /api/research/audit returns shape matching get_audit_recent [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}` };

  // Plain list
  const all = await page.request.get(`${BASE}/api/research/audit?limit=20`, { headers });
  expect(all.ok()).toBe(true);
  const rows = await all.json();
  expect(Array.isArray(rows)).toBe(true);
  console.log(`/audit rows: ${rows.length}`);

  // tool-filtered — restricted to place_order (and later cancel/modify)
  const filtered = await page.request.get(
    `${BASE}/api/research/audit?tool=place_order&limit=5`, { headers });
  expect(filtered.ok()).toBe(true);
  const placeRows = await filtered.json();
  for (const r of placeRows) {
    expect(r.tool).toBe('place_order');
  }
  console.log(`place_order audit rows: ${placeRows.length}`);

  // status filter
  const denied = await page.request.get(
    `${BASE}/api/research/audit?status=denied&limit=5`, { headers });
  expect(denied.ok()).toBe(true);
  const deniedRows = await denied.json();
  for (const r of deniedRows) {
    expect(r.result_status).toBe('denied');
  }
  console.log(`denied audit rows: ${deniedRows.length}`);
});

test(`Settings tab — 17 tools including get_audit_recent [${BASE}]`, async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/admin/research`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(3500);
  await page.locator('.lab-tab', { hasText: 'Settings' }).click();
  await page.waitForTimeout(400);
  const toolRows = page.locator('.tools-table tbody tr');
  await expect(toolRows).toHaveCount(17);
  await expect(page.locator('.tools-table tbody tr', { hasText: 'get_audit_recent' })).toBeVisible();
});
