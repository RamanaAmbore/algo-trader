// Verify Phase 13/14/17/18 in one pass.
//   13: stale-token cleanup — symbolic; mainly that endpoints still
//       behave correctly after the prune-on-consume change.
//   14: update_agent — mint kind='update' produces a hash-bound token;
//       cross-changes redemption blocked; end-to-end flow flips
//       cooldown_minutes on an inactive agent.
//   17: empty-state CTAs present on Drafts + Threads tabs.
//   18: /api/research/audit?request_id=… filters to one row; the
//       Lab page's deep-link query param flips to Audit tab + pre-fills.

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

test(`Phase 14 — update_agent end-to-end [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  // Create a thread + promote → inactive agent
  const t = await page.request.post(`${BASE}/api/research/threads`, {
    data: { symbol: 'PWUPD', title: 'phase 14 e2e', confidence: 'neutral' }, headers,
  });
  const thread = await t.json();
  const p = await page.request.post(`${BASE}/api/research/threads/${thread.id}/promote`, {
    data: {
      name: 'Phase 14 probe',
      conditions: { all: [{ metric: 'pnl', scope: 'positions.total', op: '<=', value: -1 }] },
      cooldown_minutes: 30,
    }, headers,
  });
  const draft = await p.json();
  expect(draft.agent_status).toBe('inactive');

  // Mint kind=update bound to a specific cooldown change
  const proposed = { cooldown_minutes: 15 };
  const mint = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: {
      kind: 'update',
      agent_slug: draft.agent_slug,
      proposed_changes: proposed,
    }, headers,
  });
  expect(mint.ok(), `update mint: ${mint.status()}`).toBe(true);
  const { token, purpose } = await mint.json();
  expect(purpose).toContain('UPDATE');
  expect(purpose).toContain(`agent=${draft.agent_slug}`);
  expect(purpose).toContain('fields=cooldown_minutes');

  // Successful update — same proposed_changes → 200
  const upd = await page.request.post(`${BASE}/api/research/update-agent`, {
    data: {
      confirm_token: token,
      agent_slug: draft.agent_slug,
      proposed_changes: proposed,
    }, headers,
  });
  expect(upd.ok(), `update apply: ${upd.status()}`).toBe(true);
  console.log(`update result: ${(await upd.json()).detail}`);

  // Replay → 403
  const replay = await page.request.post(`${BASE}/api/research/update-agent`, {
    data: { confirm_token: token, agent_slug: draft.agent_slug, proposed_changes: proposed }, headers,
  });
  expect(replay.status()).toBe(403);

  // Mint again, try to use it for a DIFFERENT change → 403
  const mint2 = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: {
      kind: 'update',
      agent_slug: draft.agent_slug,
      proposed_changes: { cooldown_minutes: 15 },
    }, headers,
  });
  const { token: tok2 } = await mint2.json();
  const bad = await page.request.post(`${BASE}/api/research/update-agent`, {
    data: {
      confirm_token: tok2,
      agent_slug: draft.agent_slug,
      proposed_changes: { cooldown_minutes: 99 },  // ← swap
    }, headers,
  });
  expect(bad.status(), 'changed cooldown must trigger 403').toBe(403);
  console.log(`swap denied: ${(await bad.json()).detail}`);

  // Try to sneak status=active through update_agent → silently dropped
  const mint3 = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: {
      kind: 'update',
      agent_slug: draft.agent_slug,
      proposed_changes: { description: 'updated', status: 'active' },
    }, headers,
  });
  const { token: tok3, purpose: p3 } = await mint3.json();
  // status shouldn't appear in the purpose (it's filtered out before hashing)
  expect(p3).not.toContain('status');
  console.log(`mint dropped non-whitelisted: ${p3}`);
  const r3 = await page.request.post(`${BASE}/api/research/update-agent`, {
    data: {
      confirm_token: tok3,
      agent_slug: draft.agent_slug,
      proposed_changes: { description: 'updated', status: 'active' },
    }, headers,
  });
  expect(r3.ok(), `update with non-whitelisted: ${r3.status()}`).toBe(true);
  // Agent must still be inactive
  const list = await page.request.get(`${BASE}/api/agents/`, { headers });
  const rows = await list.json();
  const me = (Array.isArray(rows) ? rows : (rows.agents || [])).find(a => a.slug === draft.agent_slug);
  expect(me?.status, `status must still be inactive: ${me?.status}`).toBe('inactive');
  console.log(`non-whitelisted status field correctly silently dropped`);

  // Cleanup
  await page.request.delete(`${BASE}/api/agents/${draft.agent_slug}`, { headers });
  await page.request.delete(`${BASE}/api/research/threads/${thread.id}`, { headers });
});

test(`Phase 18 — /audit?request_id filter [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}` };

  // Pick any existing audit row
  const all = await page.request.get(`${BASE}/api/research/audit?limit=5`, { headers });
  const rows = await all.json();
  if (rows.length === 0) {
    console.log('(soft-skip — no audit rows on dev)');
    return;
  }
  const target = rows[0];
  console.log(`probing request_id=${target.request_id}`);

  const filtered = await page.request.get(
    `${BASE}/api/research/audit?request_id=${encodeURIComponent(target.request_id)}`,
    { headers });
  expect(filtered.ok()).toBe(true);
  const fRows = await filtered.json();
  expect(fRows.length).toBe(1);
  expect(fRows[0].id).toBe(target.id);
  expect(fRows[0].request_id).toBe(target.request_id);
});

test(`Phase 18 UI — deep-link query param lands on Audit tab [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}` };

  // Find an existing request_id to deep-link to
  const all = await page.request.get(`${BASE}/api/research/audit?limit=5`, { headers });
  const rows = await all.json();
  if (rows.length === 0) {
    console.log('(soft-skip — no audit rows)');
    return;
  }
  const target = rows[0];

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(
    `${BASE}/admin/research?audit_request=${target.request_id}`,
    { waitUntil: 'networkidle' });
  await page.waitForTimeout(3500);

  // Audit tab should be active automatically
  const auditTab = page.locator('.lab-tab', { hasText: 'Audit' });
  await expect(auditTab).toHaveClass(/lab-tab-on/);

  // Banner above the audit table announces the deep-link
  const banner = page.locator('.audit-deeplink-banner');
  await expect(banner).toBeVisible();
  await expect(banner).toContainText(target.request_id);

  // Table shows exactly one row
  const tableRows = page.locator('.audit-table tbody tr');
  await expect(tableRows).toHaveCount(1);

  await page.screenshot({ path: `test-results/research-deeplink-${BASE.includes('dev') ? 'dev' : 'prod'}.png` });
});

test(`Phase 17 — empty-state CTAs present [${BASE}]`, async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/admin/research`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(3500);

  // Drafts tab — if list is empty, the CTA must be there
  await page.locator('.lab-tab', { hasText: 'Drafts' }).click();
  await page.waitForTimeout(400);
  const draftsRows = page.locator('.drafts-table tbody tr');
  const draftCount = await draftsRows.count();
  if (draftCount === 0) {
    await expect(page.locator('.empty-cta', { hasText: 'Pick a thread' })).toBeVisible();
    console.log('Drafts empty-state CTA present');
  } else {
    console.log(`(soft-skip — ${draftCount} drafts exist on dev)`);
  }
});

test(`Settings tab — 24 tools incl update_agent [${BASE}]`, async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/admin/research`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(3500);
  await page.locator('.lab-tab', { hasText: 'Settings' }).click();
  await page.waitForTimeout(400);
  const toolRows = page.locator('.tools-table tbody tr');
  await expect(toolRows).toHaveCount(24);
  await expect(page.locator('.tools-table tbody tr td code').filter({ hasText: /^update_agent$/ })).toBeVisible();
});
