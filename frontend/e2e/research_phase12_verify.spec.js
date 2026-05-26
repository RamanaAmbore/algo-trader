// Verify Phase 12 — activate_agent + deactivate_agent via gated token.
//   1. Mint kind='activate' produces a purpose carrying "ACTIVATE · agent=..."
//   2. Mint kind='deactivate' produces "DEACTIVATE · agent=..."
//   3. Cross-direction redemption blocked (activate token → deactivate call → 403)
//   4. Cross-agent redemption blocked (token for agent A → call for agent B → 403)
//   5. End-to-end: create an inactive agent → mint activate token → activate
//      → verify status flipped → cleanup (back to inactive + delete)
//   6. Tools-table count = 23

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

test(`activate/deactivate mint shapes [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  const mA = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: { kind: 'activate', agent_slug: 'pw-test-agent' }, headers,
  });
  expect(mA.ok(), `activate mint: ${mA.status()}`).toBe(true);
  const a = await mA.json();
  expect(a.purpose).toContain('ACTIVATE');
  expect(a.purpose).toContain('agent=pw-test-agent');

  const mD = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: { kind: 'deactivate', agent_slug: 'pw-test-agent' }, headers,
  });
  expect(mD.ok(), `deactivate mint: ${mD.status()}`).toBe(true);
  const d = await mD.json();
  expect(d.purpose).toContain('DEACTIVATE');
  console.log(`mints: ${a.purpose} | ${d.purpose}`);

  // Missing agent_slug → 400
  const bad = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: { kind: 'activate', agent_slug: '' }, headers,
  });
  expect(bad.status()).toBe(400);
});

test(`cross-direction token rejection [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  // Mint an ACTIVATE token; try to use it for DEACTIVATE → must be 403.
  const mint = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: { kind: 'activate', agent_slug: 'pw-cross-test' }, headers,
  });
  const { token } = await mint.json();

  const bad = await page.request.post(`${BASE}/api/research/deactivate-agent`, {
    data: { confirm_token: token, agent_slug: 'pw-cross-test' }, headers,
  });
  expect(bad.status(), 'activate token → deactivate call must be 403').toBe(403);
  console.log(`cross-direction denied: ${(await bad.json()).detail}`);
});

test(`cross-agent token rejection [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  // Mint for agent A; redeem for agent B → 403.
  const mint = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: { kind: 'activate', agent_slug: 'pw-agent-A' }, headers,
  });
  const { token } = await mint.json();

  const bad = await page.request.post(`${BASE}/api/research/activate-agent`, {
    data: { confirm_token: token, agent_slug: 'pw-agent-B' }, headers,
  });
  expect(bad.status(), 'agent-A token → agent-B call must be 403').toBe(403);
  console.log(`cross-agent denied: ${(await bad.json()).detail}`);
});

test(`end-to-end activate → deactivate flow [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  // 1. Create a research thread + promote to inactive agent.
  const t = await page.request.post(`${BASE}/api/research/threads`, {
    data: { symbol: 'PW12', title: 'phase-12 e2e', confidence: 'neutral' },
    headers,
  });
  const thread = await t.json();

  const p = await page.request.post(`${BASE}/api/research/threads/${thread.id}/promote`, {
    data: {
      name: 'Phase 12 probe',
      conditions: { all: [{ metric: 'pnl', scope: 'positions.total', op: '<=', value: -1 }] },
    }, headers,
  });
  expect(p.ok(), `promote: ${p.status()}`).toBe(true);
  const draft = await p.json();
  expect(draft.agent_status).toBe('inactive');
  console.log(`created agent: ${draft.agent_slug} status=${draft.agent_status}`);

  // 2. Mint activate token + activate.
  const mintA = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: { kind: 'activate', agent_slug: draft.agent_slug }, headers,
  });
  const { token: tokA } = await mintA.json();
  const act = await page.request.post(`${BASE}/api/research/activate-agent`, {
    data: { confirm_token: tokA, agent_slug: draft.agent_slug }, headers,
  });
  expect(act.ok(), `activate: ${act.status()}`).toBe(true);
  const actJ = await act.json();
  expect(actJ.status).toBe('active');
  console.log(`activated: ${actJ.detail}`);

  // 3. Replay → 403 (token consumed).
  const replay = await page.request.post(`${BASE}/api/research/activate-agent`, {
    data: { confirm_token: tokA, agent_slug: draft.agent_slug }, headers,
  });
  expect(replay.status(), 'replay must be 403').toBe(403);

  // 4. Verify the audit row landed. Note: the replay step above also
  // wrote a 'denied' row for the same slug; filter to the 'ok' row
  // explicitly so we don't pick up the more-recent denial.
  const audit = await page.request.get(
    `${BASE}/api/research/audit?tool=activate_agent&status=ok&limit=20`, { headers });
  const auditRows = await audit.json();
  const myRow = auditRows.find(r => (r.args_redacted || {}).agent_slug === draft.agent_slug);
  expect(myRow, 'ok audit row must be present').toBeTruthy();
  expect(myRow.result_status).toBe('ok');

  // 5. Cleanup — mint deactivate token + flip back, then delete.
  const mintD = await page.request.post(`${BASE}/api/research/confirm-token`, {
    data: { kind: 'deactivate', agent_slug: draft.agent_slug }, headers,
  });
  const { token: tokD } = await mintD.json();
  const deact = await page.request.post(`${BASE}/api/research/deactivate-agent`, {
    data: { confirm_token: tokD, agent_slug: draft.agent_slug }, headers,
  });
  expect(deact.ok()).toBe(true);
  await page.request.delete(`${BASE}/api/agents/${draft.agent_slug}`, { headers });
  await page.request.delete(`${BASE}/api/research/threads/${thread.id}`, { headers });
});

test(`Settings tab — 23 tools + new kinds in selector [${BASE}]`, async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/admin/research`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(3500);
  await page.locator('.lab-tab', { hasText: 'Settings' }).click();
  await page.waitForTimeout(400);
  // Tools table
  const toolRows = page.locator('.tools-table tbody tr');
  await expect(toolRows).toHaveCount(23);
  // Exact-match via the inner <code> cell so 'activate_agent' doesn't
  // also match 'deactivate_agent'.
  await expect(page.locator('.tools-table tbody tr td code', { hasText: /^activate_agent$/ })).toBeVisible();
  await expect(page.locator('.tools-table tbody tr td code', { hasText: /^deactivate_agent$/ })).toBeVisible();
  // Kind selector is the custom Select component — open + count options
  const kindTrigger = page.locator('.mint-grid .rbq-select-trigger').first();
  await kindTrigger.click();
  await page.waitForTimeout(150);
  const kindOptions = await page.locator('.rbq-select-option').allTextContents();
  expect(kindOptions.length).toBe(5);
  expect(kindOptions.join(' ')).toMatch(/ACTIVATE/);
  expect(kindOptions.join(' ')).toMatch(/DEACTIVATE/);
  // Pick ACTIVATE agent → form should swap Account for Agent slug
  await page.locator('.rbq-select-option', { hasText: /^ACTIVATE agent$/ }).first().click();
  await page.waitForTimeout(150);
  const labels = await page.locator('.mint-grid label span').allTextContents();
  expect(labels).toContain('Agent slug');
  expect(labels).not.toContain('Account');
});
