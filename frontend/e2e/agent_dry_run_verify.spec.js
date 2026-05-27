// Verify the /api/agents/{slug}/dry-run endpoint returns 200 + a
// well-shaped body (Task #30 / Phase 22 follow-up).
//
// Background: this endpoint was 500ing on dev because two import
// sites in agents.py referenced `V2Context` while the actual class
// in agent_evaluator.py is named `Context`. The ImportError fired
// before the route's try/except could catch it, so Litestar served
// a bare 500 with no traceback. Fix renamed both call sites.

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

test(`dry-run returns 200 with valid shape on a built-in agent [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}` };

  // Pick a known-existing system agent. Built-ins are seeded on every
  // boot so this slug is always present.
  const list = await page.request.get(`${BASE}/api/agents/`, { headers });
  expect(list.ok(), `agents list: ${list.status()}`).toBe(true);
  const rows = await list.json();
  const agents = Array.isArray(rows) ? rows : (rows.agents || []);
  const candidate = agents.find(a => a.slug && a.slug.startsWith('loss-')) || agents[0];
  expect(candidate, 'at least one agent in list').toBeTruthy();

  const r = await page.request.get(`${BASE}/api/agents/${candidate.slug}/dry-run`, { headers });
  const body = await r.json().catch(() => ({}));
  console.log(`dry-run ${candidate.slug} → HTTP ${r.status()}: ${JSON.stringify(body).slice(0, 200)}`);
  expect(r.status(), `dry-run should return 200 — got ${r.status()}: ${body.detail || ''}`).toBe(200);

  // Shape contract
  expect(body.agent_slug).toBe(candidate.slug);
  expect(body).toHaveProperty('matches');
  expect(Array.isArray(body.matches)).toBe(true);
  expect(body).toHaveProperty('would_fire');
  expect(typeof body.would_fire).toBe('boolean');
  expect(body).toHaveProperty('evaluated_at');
  // blocked_by may be null or one of the gate names
  if (body.blocked_by != null) {
    expect(['schedule', 'cooldown', 'fire_at_time', 'blackout', 'debounce', 'eval_error'])
      .toContain(body.blocked_by);
  }
});

test(`dry-run on unknown slug returns 404 [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}` };
  const r = await page.request.get(`${BASE}/api/agents/__definitely-not-an-agent__/dry-run`, { headers });
  expect(r.status()).toBe(404);
});
