// Verify Phase 22 — tags + blackout_windows round-trip via promote.
// Dry-run endpoint verification deferred (separate investigation).

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

test(`Phase 22 — tags + blackout_windows round-trip [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  const t = await page.request.post(`${BASE}/api/research/threads`, {
    data: { symbol: 'PWPH22', title: 'phase 22 tags+windows', confidence: 'neutral' },
    headers,
  });
  const thread = await t.json();
  const p = await page.request.post(`${BASE}/api/research/threads/${thread.id}/promote`, {
    data: {
      name: 'Phase 22 probe',
      conditions: { all: [{ metric: 'pnl', scope: 'positions.total', op: '<=', value: -1 }] },
      tags: ['iron-condor', 'nifty'],
      blackout_windows: [
        { start: '12:00', end: '13:00' },
        { start: '23:00', end: '01:00' },  // crossing-midnight
      ],
    }, headers,
  });
  expect(p.ok(), `promote: ${p.status()}`).toBe(true);
  const draft = await p.json();

  const ag = await page.request.get(`${BASE}/api/agents/${draft.agent_slug}`, { headers });
  const agentData = await ag.json();
  expect(agentData.tags).toEqual(['iron-condor', 'nifty']);
  expect(agentData.blackout_windows).toHaveLength(2);
  expect(agentData.blackout_windows[1]).toEqual({ start: '23:00', end: '01:00' });
  console.log(`tags + blackouts round-tripped: tags=${agentData.tags.join(',')} windows=${agentData.blackout_windows.length}`);

  // Cleanup
  await page.request.delete(`${BASE}/api/agents/${draft.agent_slug}`, { headers });
  await page.request.delete(`${BASE}/api/research/threads/${thread.id}`, { headers });
});

test(`AgentInfo on existing built-ins shows new fields [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}` };
  const r = await page.request.get(`${BASE}/api/agents/`, { headers });
  expect(r.ok()).toBe(true);
  const data = await r.json();
  const rows = Array.isArray(data) ? data : (data.agents || []);
  const sample = rows[0];
  expect('tags' in sample, 'AgentInfo carries tags').toBe(true);
  expect('blackout_windows' in sample, 'AgentInfo carries blackout_windows').toBe(true);
  expect(Array.isArray(sample.tags)).toBe(true);
  expect(Array.isArray(sample.blackout_windows)).toBe(true);
  console.log(`AgentInfo shape verified — tags + blackout_windows present on ${rows.length} agents`);
});
