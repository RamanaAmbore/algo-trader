/**
 * console_event_token_submit.spec.js — Sprint 14
 *
 * Audit finding: `AgentCliEditor`'s compiler has correctly produced
 * `kind:'event'` (Sprint 12) and `kind:'token'` (Sprint 10) payloads for
 * months, but NOTHING on the real `/console` page ever rendered a submit
 * button for either kind — `console/+page.svelte` only gated its submit
 * button on `kind === 'agent'`/`kind === 'bracket'`, and no
 * `submitConsoleToken` function existed at all. Every prior test either
 * hand-built an object and called `submitConsoleAgent` directly (skipping
 * the page's own gating logic) or was a pure Vitest compiler test —
 * nothing ever actually typed text into the real page and clicked
 * Submit. This spec drives the real page end-to-end to close that gap.
 *
 * Covers (Sprint 14 "Done when" criteria):
 *   1. Typing a complete `ALERT log_tag=..., render=..., telegram
 *      #name="..."` statement into the real AgentCliEditor textarea on
 *      /console shows a "Create agent" submit button; clicking it creates
 *      a real `kind:'event'` Agent row (verified via GET /api/agents/).
 *   2. Typing a complete `token(kind=notify) telegram #name="..."`
 *      statement shows a "Create token" submit button; clicking it
 *      creates a real `kind:'template'` Agent row (verified via GET
 *      /api/agents/ — Sprint D2, Agent/Bracket/Template unification
 *      phase, retired the separate /api/admin/fragments route a Token
 *      used to be created/read through).
 *
 * Environment: localhost (workshop branch) — vite dev server proxies
 * /api to dev.ramboq.com (see frontend/vite.config.js), so API calls in
 * this spec hit the real dev database.
 * Auth: dedicated trader-role test account (same account + direct-API
 * login pattern as e2e/automation_cli_grammar.spec.js — the default
 * `rambo`/admin fixture account lacks `manage_own_agents`, required for
 * POST /api/agents/).
 * Viewport: chromium-desktop only (same convention as the sibling
 * automation_cli_grammar.spec.js — CLI authoring is not a mobile-first
 * surface).
 */

import { test, expect, request } from '@playwright/test';

test.use({ projects: ['chromium-desktop'] });

const AGENT_TEST_USER = process.env.PLAYWRIGHT_AGENT_USER || 'playwright_agent_test';
const AGENT_TEST_PASS = process.env.PLAYWRIGHT_AGENT_PASS || 'Pw4Test!Agents2026x';
// Distinct cache file from automation_cli_grammar.spec.js's own
// e2e/.auth/state_agent_test.json — same underlying account, but a
// separate file avoids any cross-spec-file concurrent write race on the
// shared cache (Playwright may run both spec files' workers at once).
const CONSOLE_TEST_STATE_FILE = 'e2e/.auth/state_console_e2e_test.json';
const CONSOLE_TEST_CACHE_TTL_MS = 20 * 60 * 60 * 1000; // mirrors global-setup.js's 20h TTL

/** Fetch (or reuse the on-disk cached) token exactly ONCE per test-file
 *  run — called from beforeAll, not beforeEach, for the same reason
 *  documented in automation_cli_grammar.spec.js (avoids tripping the
 *  login rate limit). */
async function fetchConsoleTestToken() {
  const { readFileSync, writeFileSync, mkdirSync, existsSync } = await import('fs');
  if (existsSync(CONSOLE_TEST_STATE_FILE)) {
    try {
      const saved = JSON.parse(readFileSync(CONSOLE_TEST_STATE_FILE, 'utf-8'));
      if (saved?.token && saved?.cached_at &&
          Date.now() - new Date(saved.cached_at).getTime() < CONSOLE_TEST_CACHE_TTL_MS) {
        return saved.token;
      }
    } catch { /* corrupt/missing cache — fetch fresh below */ }
  }
  const ctx = await request.newContext({ baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174' });
  try {
    const res = await ctx.post('/api/auth/login', { data: { username: AGENT_TEST_USER, password: AGENT_TEST_PASS } });
    if (!res.ok()) throw new Error(`console-test login failed ${res.status()}: ${await res.text()}`);
    const token = (await res.json()).access_token;
    mkdirSync('e2e/.auth', { recursive: true });
    writeFileSync(CONSOLE_TEST_STATE_FILE, JSON.stringify({ token, cached_at: new Date().toISOString() }, null, 2));
    return token;
  } finally {
    await ctx.dispose();
  }
}

test.describe('console — event-agent and Token submission (Sprint 14 audit gap)', () => {
  // Serial — both tests share one worker/token fetch, same pattern as
  // automation_cli_grammar.spec.js.
  test.describe.configure({ mode: 'serial' });

  /** @type {string} */
  let consoleTestToken;

  test.beforeAll(async () => {
    consoleTestToken = await fetchConsoleTestToken();
  });

  test.beforeEach(async ({ page }) => {
    await page.addInitScript((tok) => { sessionStorage.setItem('ramboq_token', tok); }, consoleTestToken);
    await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${consoleTestToken}` });
    await page.goto('/console');
    await page.waitForLoadState('networkidle');
  });

  test.afterEach(async ({ page }) => {
    // Cleanup — delete anything this spec created, via API. A Token is
    // just an Agent(kind='template') row (Sprint D2, Agent/Bracket/
    // Template unification phase) — the SAME /api/agents/ delete the
    // event-agent cleanup above already uses covers both.
    try {
      const agentsResp = await page.request.get('/api/agents/');
      const agents = await agentsResp.json();
      for (const agent of agents) {
        if (agent.slug?.includes('zzz-console-e2e')) {
          try { await page.request.delete(`/api/agents/${agent.slug}`); } catch {}
        }
      }
    } catch {}
  });

  test('typing an ALERT (event-agent) statement shows "Create agent" and submits a real event-kind Agent row', async ({ page }) => {
    const uniqueName = `ZZZ Console E2E Event ${Date.now()}`;
    // log_tag must be a REAL catalog tag (backend validates against
    // `LOG_TAG_TOKENS` — see event_agents.py:validate_seed_spec) — 'error'
    // is a real built-in tag, same as the seeded ERROR_AGENT.
    const statement = `ALERT log_tag=error, min_level=ERROR, render=error, telegram #name="${uniqueName}"`;

    const cliTextarea = page.locator('.agent-cli-textarea');
    await expect(cliTextarea).toBeVisible({ timeout: 10000 });
    await cliTextarea.fill(statement);

    // AgentCliEditor debounces compile by 150ms; give the catalog fetch +
    // debounce + Svelte reactivity a little headroom.
    const createAgentBtn = page.locator('.console-agent-cli-btn', { hasText: 'Create agent' });
    await expect(createAgentBtn).toBeVisible({ timeout: 10000 });

    // JSON preview (AgentCliEditor's own previewPayload, Sprint 14 fix)
    // must now actually render for an event-kind compile instead of
    // staying blank.
    await expect(page.locator('.agent-cli-preview-json')).toBeVisible();
    await expect(page.locator('.agent-cli-preview-json')).toContainText('"kind": "event"');

    await createAgentBtn.click();

    // Submit clears the textarea on success (console/+page.svelte
    // `_submitAgentCliAgent`).
    await expect(cliTextarea).toHaveValue('', { timeout: 10000 });

    // ── Verify a real row was created, via the agents list API ──
    const agentsResp = await page.request.get('/api/agents/');
    expect(agentsResp.ok()).toBe(true);
    const agents = await agentsResp.json();
    const created = agents.find((a) => a.name === uniqueName);
    expect(created).toBeTruthy();
    expect(created.kind).toBe('event');
    expect(created.slug).toContain('zzz-console-e2e-event');
  });

  test('typing a token(kind=notify) statement shows "Create token" and submits a real Token row', async ({ page }) => {
    const uniqueName = `zzz-console-e2e-token-${Date.now()}`;
    const statement = `token(kind=notify) telegram #name="${uniqueName}"`;

    const cliTextarea = page.locator('.agent-cli-textarea');
    await expect(cliTextarea).toBeVisible({ timeout: 10000 });
    await cliTextarea.fill(statement);

    const createTokenBtn = page.locator('.console-agent-cli-btn', { hasText: 'Create token' });
    await expect(createTokenBtn).toBeVisible({ timeout: 10000 });

    await expect(page.locator('.agent-cli-preview-json')).toBeVisible();
    await expect(page.locator('.agent-cli-preview-json')).toContainText('"kind": "notify"');

    await createTokenBtn.click();
    await expect(cliTextarea).toHaveValue('', { timeout: 10000 });

    // ── Verify a real row was created, via the unified agents list API
    //    (Sprint D2 — a Token is now an Agent(kind='template') row, not
    //    a separate agent_fragments row) ──
    const agentsResp = await page.request.get('/api/agents/');
    expect(agentsResp.ok()).toBe(true);
    const agents = await agentsResp.json();
    const created = agents.find((a) => a.slug === uniqueName);
    expect(created).toBeTruthy();
    expect(created.kind).toBe('template');
    expect(created.events).toEqual([{ channel: 'telegram', enabled: true }]);
  });
});
