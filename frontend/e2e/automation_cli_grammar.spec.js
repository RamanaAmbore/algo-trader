/**
 * automation_cli_grammar.spec.js — Sprint 4 (CLI editor) → trimmed Sprint 9
 *
 * Sprint 9 (2026-10) removed the Structured↔CLI toggle and AgentCliEditor
 * wiring from THIS page (/automation) — CLI authoring now lives at
 * /console (a separate surface, separate spec). Of the original 6
 * scenarios, 4 directly exercised the now-gone CLI tab (round-trip save,
 * invalid-input error list, no-touch stability, bare order statement) and
 * were deleted outright rather than adapted, since the UI they asserted
 * on no longer exists on this page.
 *
 * Remaining 2 scenarios still apply to /automation's Structured-only
 * editor and were kept (test 4 adapted — no more "switch to Structured"
 * click, since Structured is now the only mode):
 * 4. cli_source clearing: an agent that carries a `cli_source` string
 *    (e.g. authored at /console) gets it cleared to "" on any save from
 *    this page's Structured editor — the invariant documented in
 *    `_buildEditPayload` (+page.svelte).
 * 5. Delete unaffected: delete still works for a CLI-authored agent.
 *
 * Environment: localhost (workshop branch)
 * Auth: loginAsAdmin fixture, overridden to a dedicated trader-role test
 * account (`playwright_agent_test`, dev DB only) — agent create/update/
 * delete routes require the `manage_own_agents` capability
 * (backend/api/rbac.py), which is scoped to designated/trader roles only.
 * The default `rambo` fixture account is role=admin (deliberately excluded
 * from this capability — "operational support, no trading rights") and
 * 403s on every agent-management call; this is not a regression,
 * confirmed against a pre-existing, unrelated agent spec hitting the same
 * 403 with the default account.
 * Viewport: chromium-desktop only
 *
 * Key pattern: All row interactions scoped to one unique agent via .algo-status-card filter
 * to avoid strict-mode selector violations on repeated row markup.
 */

import { test, expect, request } from '@playwright/test';

test.use({ projects: ['chromium-desktop'] });

const AGENT_TEST_USER = process.env.PLAYWRIGHT_AGENT_USER || 'playwright_agent_test';
const AGENT_TEST_PASS = process.env.PLAYWRIGHT_AGENT_PASS || 'Pw4Test!Agents2026x';
const AGENT_TEST_STATE_FILE = 'e2e/.auth/state_agent_test.json';
const AGENT_TEST_CACHE_TTL_MS = 20 * 60 * 60 * 1000; // mirrors global-setup.js's 20h TTL

/**
 * Dedicated login path for the trader-role agent-test account, bypassing
 * `loginAsAdmin`'s /signin form flow entirely (same direct-API pattern as
 * global-setup.js) — the rambo-keyed `e2e/.auth/state.json` cache can't
 * also hold this second user, and repeatedly driving the real signin form
 * once per test trips the login rate limit well before all tests finish.
 * Caches its own token to a sibling state file, reused across runs.
 */
/** Fetch (or reuse the on-disk cached) token exactly ONCE per test-file run —
 *  called from beforeAll, not beforeEach, so tests never race each other
 *  into concurrent /api/auth/login calls (that race is what tripped the
 *  429 rate limit even with the disk cache in place: several tests'
 *  beforeEach hooks could all miss the not-yet-written cache at once). */
async function fetchAgentTestToken() {
  const { readFileSync, writeFileSync, mkdirSync, existsSync } = await import('fs');
  if (existsSync(AGENT_TEST_STATE_FILE)) {
    try {
      const saved = JSON.parse(readFileSync(AGENT_TEST_STATE_FILE, 'utf-8'));
      if (saved?.token && saved?.cached_at &&
          Date.now() - new Date(saved.cached_at).getTime() < AGENT_TEST_CACHE_TTL_MS) {
        return saved.token;
      }
    } catch { /* corrupt/missing cache — fetch fresh below */ }
  }
  const ctx = await request.newContext({ baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174' });
  try {
    const res = await ctx.post('/api/auth/login', { data: { username: AGENT_TEST_USER, password: AGENT_TEST_PASS } });
    if (!res.ok()) throw new Error(`agent-test login failed ${res.status()}: ${await res.text()}`);
    const token = (await res.json()).access_token;
    mkdirSync('e2e/.auth', { recursive: true });
    writeFileSync(AGENT_TEST_STATE_FILE, JSON.stringify({ token, cached_at: new Date().toISOString() }, null, 2));
    return token;
  } finally {
    await ctx.dispose();
  }
}

test.describe('automation — Structured editor cli_source invariant (post-Sprint-9)', () => {
  // Serial, not parallel: Playwright's default config runs each test in
  // this file on its OWN worker process, so beforeAll alone doesn't dedupe
  // the login call across workers. Serial mode runs both tests in ONE
  // worker, so fetchAgentTestToken() below genuinely executes once.
  test.describe.configure({ mode: 'serial' });

  /** @type {string} */
  let agentTestToken;

  test.beforeAll(async () => {
    agentTestToken = await fetchAgentTestToken();
  });

  test.beforeEach(async ({ page }) => {
    await page.addInitScript((tok) => { sessionStorage.setItem('ramboq_token', tok); }, agentTestToken);
    await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${agentTestToken}` });
    await page.goto('/automation');
    await page.waitForLoadState('networkidle');
  });

  test.afterEach(async ({ page }) => {
    // Cleanup: delete test agents via API
    try {
      const agentsResp = await page.request.get('/api/agents/');
      const agents = await agentsResp.json();
      for (const agent of agents) {
        if (agent.slug?.includes('zzz-cli-test')) {
          try {
            await page.request.delete(`/api/agents/${agent.slug}`);
          } catch {}
        }
      }
    } catch {}
  });

  test('cli_source clearing: agent carrying cli_source → edit → save → cli_source=""', async ({ page }) => {
    const testName = `ZZZ CLI Test Source ${Date.now()}`;
    const testSlug = `zzz-cli-test-source-${Date.now()}`;
    const cliStatement = 'WHEN avail_margin@funds.total >= 100000 ALERT email DO nop';

    // ── Create agent via API carrying a cli_source (as /console would) ──
    await page.request.post('/api/agents/', {
      data: {
        slug: testSlug,
        kind: 'threshold',
        name: testName,
        conditions: { metric: 'avail_margin', scope: 'funds.total', op: '>=', value: 100000 },
        events: [{ channel: 'email', enabled: true }],
        actions: [],
        scope: 'total',
        schedule: 'market_hours',
        cooldown_minutes: 30,
        trade_mode: 'paper',
        cli_source: cliStatement, // Pre-set cli_source on creation
      },
    });

    // ── Reload and edit ──
    await page.reload();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    const agentCard = page.locator('.algo-status-card').filter({ hasText: testName });
    await expect(agentCard).toBeVisible({ timeout: 10000 });
    await agentCard.getByRole('button').first().click();
    await page.waitForTimeout(300);
    await agentCard.getByRole('button', { name: 'Edit' }).click();
    await page.waitForTimeout(600);

    // No Structured/CLI toggle exists on this page anymore (Sprint 9) —
    // the editor IS the Structured form. "Edit something" = flip the
    // Email checkbox (channels are a checkbox grid, not a raw textarea).
    const emailCheckbox = page.locator('.channel-row').filter({ hasText: 'Email' }).locator('.channel-check');
    await emailCheckbox.click();
    await page.waitForTimeout(200);

    // ── Capture the PUT request to verify cli_source is "" ──
    let cliSourceInRequest = null;
    page.on('request', async r => {
      if (r.method() === 'PUT' && r.url().includes(`/api/agents/${testSlug}`)) {
        try {
          const body = await r.postDataJSON();
          cliSourceInRequest = body.cli_source;
        } catch {}
      }
    });

    await page.getByRole('button', { name: 'Save' }).first().click();
    await page.waitForTimeout(500);

    // ── Verify cli_source was sent as empty string ──
    expect(cliSourceInRequest).toBe('');
  });

  test('Delete unaffected: delete still works for a CLI-authored agent', async ({ page }) => {
    const testName = `ZZZ CLI Test Delete ${Date.now()}`;
    const testSlug = `zzz-cli-test-delete-${Date.now()}`;

    // ── Create agent via API with cli_source ──
    await page.request.post('/api/agents/', {
      data: {
        slug: testSlug,
        kind: 'threshold',
        name: testName,
        conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -50000 },
        events: [{ channel: 'log', enabled: true }],
        actions: [{ type: 'emit_log', params: { message: 'Test' } }],
        scope: 'total',
        schedule: 'market_hours',
        cooldown_minutes: 30,
        trade_mode: 'paper',
        cli_source: 'WHEN always ALERT log DO nop',
      },
    });

    // ── Reload and verify agent exists ──
    await page.reload();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    const agentCard = page.locator('.algo-status-card').filter({ hasText: testName });
    await expect(agentCard).toBeVisible({ timeout: 10000 });

    // ── Delete via API ──
    const deleteResp = await page.request.delete(`/api/agents/${testSlug}`);
    const deleteStatus = deleteResp.status();

    expect([200, 204]).toContain(deleteStatus);

    // ── Verify agent no longer in list ──
    await page.reload();
    await page.waitForLoadState('networkidle');

    const deletedCard = page.locator('.algo-status-card').filter({ hasText: testName });
    await expect(deletedCard).not.toBeVisible({ timeout: 5000 });
  });
});
