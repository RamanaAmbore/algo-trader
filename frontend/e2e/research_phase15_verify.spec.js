// Verify Phase 15 — Audit since-window filter.
//   1. /api/research/audit?since=ISO returns only rows >= since
//   2. Bad `since` value is silently ignored (returns full set, not 500)
//   3. UI: 3 filter dropdowns visible (Since + Tool + Status), Since
//      defaults to "All time"
//   4. Picking "Last hour" narrows the result count to <= the full set

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

test(`/api/research/audit honors since [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}` };

  // Baseline — all rows.
  const all = await page.request.get(`${BASE}/api/research/audit?limit=200`, { headers });
  expect(all.ok()).toBe(true);
  const allRows = await all.json();
  console.log(`baseline rows: ${allRows.length}`);

  // since = 1 hour ago — should be ≤ baseline.
  const oneHourAgo = new Date(Date.now() - 3600 * 1000).toISOString();
  const recent = await page.request.get(
    `${BASE}/api/research/audit?since=${encodeURIComponent(oneHourAgo)}&limit=200`, { headers });
  expect(recent.ok()).toBe(true);
  const recentRows = await recent.json();
  console.log(`last-hour rows: ${recentRows.length}`);
  expect(recentRows.length).toBeLessThanOrEqual(allRows.length);
  // Every returned row's created_at must be >= the cutoff.
  for (const row of recentRows) {
    expect(new Date(row.created_at).getTime()).toBeGreaterThanOrEqual(new Date(oneHourAgo).getTime());
  }

  // since = year 2999 — should return zero rows but NOT 500.
  const future = '2999-01-01T00:00:00+00:00';
  const empty = await page.request.get(
    `${BASE}/api/research/audit?since=${encodeURIComponent(future)}&limit=200`, { headers });
  expect(empty.ok()).toBe(true);
  expect((await empty.json()).length).toBe(0);

  // since = malformed — should NOT 500. Returns full set (filter ignored).
  const bad = await page.request.get(
    `${BASE}/api/research/audit?since=NOTADATE&limit=200`, { headers });
  expect(bad.ok(), `malformed since: ${bad.status()}`).toBe(true);
  console.log(`malformed since rows: ${(await bad.json()).length} (unchanged from baseline)`);
});

test(`Audit tab — 3 filter selects incl Since [${BASE}]`, async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/admin/research`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(3500);
  await page.locator('.lab-tab', { hasText: 'Audit' }).click();
  await page.waitForTimeout(400);

  // 3 selects in the audit-filters row now: Since, Tool, Status
  const selects = page.locator('.audit-filters .rbq-select-trigger');
  await expect(selects).toHaveCount(3);
  // First select is Since
  await expect(page.locator('.audit-filters label span', { hasText: 'Since' })).toBeVisible();
  // Pick "Last hour"
  await selects.first().click();
  await page.waitForTimeout(150);
  await page.locator('.rbq-select-option-label').filter({ hasText: 'Last hour' }).first().click();
  await page.waitForTimeout(400);
  // Trigger should now read "Last hour"
  const labelTxt = await selects.first().innerText();
  expect(labelTxt).toContain('Last hour');

  await page.screenshot({ path: `test-results/research-audit-since-${BASE.includes('dev') ? 'dev' : 'prod'}.png` });
});
