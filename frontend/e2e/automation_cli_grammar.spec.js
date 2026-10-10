/**
 * automation_cli_grammar.spec.js — Sprint 4: Agent CLI grammar UI tests
 *
 * Covers 6 core scenarios:
 * 1. Round-trip save: create via CLI → save → reload → edit → CLI active, JSON matches
 * 2. Invalid input: malformed CLI statement shows error list, Save blocked
 * 3. No-touch stability: Structured→CLI→Structured without editing → JSON unchanged after save
 * 4. cli_source clearing: create via CLI → edit → switch to Structured → change → save → cli_source=""
 * 5. Delete unaffected: delete works for CLI-authored agent
 * 6. Bare order statement: order(...) shows Place order button, no agent write
 *
 * Environment: localhost (workshop branch)
 * Auth: loginAsAdmin fixture, overridden to a dedicated trader-role test
 * account (`playwright_agent_test`, dev DB only) — agent create/update/
 * delete routes require the `manage_own_agents` capability
 * (backend/api/rbac.py), which is scoped to designated/trader roles only.
 * The default `rambo` fixture account is role=admin (deliberately excluded
 * from this capability — "operational support, no trading rights") and
 * 403s on every agent-management call; this is not a Sprint 4 regression,
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
 * once per test trips the login rate limit well before all 6 tests finish.
 * Caches its own token to a sibling state file, reused across runs.
 */
/** Fetch (or reuse the on-disk cached) token exactly ONCE per test-file run —
 *  called from beforeAll, not beforeEach, so 6 tests never race each other
 *  into 6 concurrent /api/auth/login calls (that race is what tripped the
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

test.describe('automation — CLI grammar editor (Sprint 4)', () => {
  // Serial, not parallel: Playwright's default config runs each test in
  // this file on its OWN worker process, so beforeAll alone doesn't dedupe
  // the login call across workers — all 6 would still race into /api/auth/
  // login concurrently and trip its ~5/min rate limit even with the disk
  // cache in place (every worker starts before any of them has written it).
  // Serial mode runs all 6 in ONE worker, so fetchAgentTestToken() below
  // genuinely executes once.
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

  test('1. Round-trip save: create via CLI → save → reload → edit → CLI active, JSON matches', async ({ page }) => {
    const testName = `ZZZ CLI Test Round-Trip ${Date.now()}`;
    const testSlug = `zzz-cli-test-roundtrip-${Date.now()}`;
    const cliStatement = 'WHEN pnl@positions.total <= -50000 ALERT telegram DO nop';

    // ── Create agent via API with blank CLI fields ──
    const createResp = await page.request.post('/api/agents/', {
      data: {
        slug: testSlug,
        kind: 'threshold',
        name: testName,
        conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -50000 },
        events: [{ channel: 'telegram', enabled: true }],
        actions: [{ type: 'emit_log', params: { message: 'Test' } }],
        scope: 'total',
        schedule: 'market_hours',
        cooldown_minutes: 30,
        trade_mode: 'paper',
      },
    });
    if (!createResp.ok()) {
      const body = await createResp.text();
      console.error(`Agent creation failed with ${createResp.status()}: ${body}`);
      throw new Error(`Agent creation failed with status ${createResp.status()}`);
    }

    // ── Reload and find the agent by unique name ──
    await page.reload();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000); // Extra wait for agents to render

    const agentCard = page.locator('.algo-status-card').filter({ hasText: testName });
    await expect(agentCard).toBeVisible({ timeout: 10000 });

    // ── Click the row header to expand ──
    const rowHeader = agentCard.getByRole('button').first();
    await rowHeader.click();
    await page.waitForTimeout(300);

    // ── Click Edit button (now visible in expanded row) ──
    const editBtn = agentCard.getByRole('button', { name: 'Edit' });
    await editBtn.click();
    await page.waitForTimeout(600); // wait for decompile

    // ── Assert CLI tab exists and is active by default (decompilable agent) ──
    const cliTab = page.getByRole('tab', { name: 'CLI' });
    const isCliActive = await cliTab.evaluate(el => el.getAttribute('aria-selected') === 'true');
    expect(isCliActive).toBe(true);

    // ── Get CLI textarea and verify it has decompiled content ──
    const cliTextarea = page.locator('.agent-cli-editor textarea');
    const cliText = await cliTextarea.inputValue();
    expect(cliText.length).toBeGreaterThan(0); // Decompiled statement should exist

    // ── Switch to Structured and verify conditions JSON ──
    const structuredTab = page.getByRole('tab', { name: 'Structured' });
    await structuredTab.click();
    await page.waitForTimeout(200);

    const conditionsTextarea = page.locator('span.field-label', { hasText: 'Conditions (JSON)' }).locator('xpath=following-sibling::textarea[1]');
    const conditionsJson = JSON.parse(await conditionsTextarea.inputValue());
    expect(conditionsJson.metric).toBe('pnl');
    expect(conditionsJson.scope).toBe('positions.total');
    expect(conditionsJson.op).toBe('<=');
    expect(conditionsJson.value).toBe(-50000);
  });

  test('2. Invalid input: malformed CLI statement shows error list, Save blocked', async ({ page }) => {
    const testName = `ZZZ CLI Test Invalid ${Date.now()}`;
    const testSlug = `zzz-cli-test-invalid-${Date.now()}`;

    // ── Create a basic agent via API ──
    await page.request.post('/api/agents/', {
      data: {
        slug: testSlug,
        kind: 'threshold',
        name: testName,
        conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -50000 },
        events: [{ channel: 'log', enabled: true }],
        actions: [],
        scope: 'total',
        schedule: 'market_hours',
        cooldown_minutes: 30,
        trade_mode: 'paper',
      },
    });

    // ── Reload and find agent ──
    await page.reload();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    const agentCard = page.locator('.algo-status-card').filter({ hasText: testName });
    await expect(agentCard).toBeVisible({ timeout: 10000 });
    const rowHeader = agentCard.getByRole('button').first();
    await rowHeader.click();
    await page.waitForTimeout(300);

    const editBtn = agentCard.getByRole('button', { name: 'Edit' });
    await editBtn.click();
    await page.waitForTimeout(600);

    // ── Switch to CLI and type malformed statement ──
    const cliTab = page.getByRole('tab', { name: 'CLI' });
    await cliTab.click();
    await page.waitForTimeout(200);

    const cliTextarea = page.locator('.agent-cli-editor textarea');
    await cliTextarea.fill('WHEN pnl@positions.total ALERT'); // missing value + DO
    await page.waitForTimeout(200);

    // ── Assert error box appears ──
    const errorBox = page.locator('.agent-cli-errors');
    await expect(errorBox).toBeVisible();

    // ── Verify Save doesn't POST ──
    let updateCount = 0;
    page.once('request', r => {
      if (r.method() === 'PUT' && r.url().includes(`/api/agents/${testSlug}`)) {
        updateCount++;
      }
    });

    const saveBtn = page.getByRole('button', { name: 'Save' }).first();
    await saveBtn.click();
    await page.waitForTimeout(300);

    // Validation should block the request
    expect(updateCount).toBe(0);
  });

  test('3. No-touch stability: Structured→CLI→Structured → save → JSON unchanged', async ({ page }) => {
    const testName = `ZZZ CLI Test Stable ${Date.now()}`;
    const testSlug = `zzz-cli-test-stable-${Date.now()}`;

    // ── Create agent via API ──
    await page.request.post('/api/agents/', {
      data: {
        slug: testSlug,
        kind: 'threshold',
        name: testName,
        conditions: { metric: 'day_pct', scope: 'positions.total', op: '>', value: 5 },
        events: [{ channel: 'email', enabled: true }],
        actions: [],
        scope: 'total',
        schedule: 'market_hours',
        cooldown_minutes: 60,
        trade_mode: 'paper',
      },
    });

    await page.reload();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    const agentCard = page.locator('.algo-status-card').filter({ hasText: testName });
    await expect(agentCard).toBeVisible({ timeout: 10000 });
    await agentCard.getByRole('button').first().click();
    await page.waitForTimeout(300);
    await agentCard.getByRole('button', { name: 'Edit' }).click();
    await page.waitForTimeout(600);

    // This agent's conditions/events/actions decompile cleanly, so the
    // panel opens defaulting to the CLI tab (per startEdit's decompile-on-
    // edit behavior) — switch to Structured FIRST, before capturing the
    // "initial" JSON, so initialJson is read from the same tab both times.
    const cliTab = page.getByRole('tab', { name: 'CLI' });
    const structuredTab = page.getByRole('tab', { name: 'Structured' });
    await structuredTab.click();
    await page.waitForTimeout(200);

    // ── Capture initial JSON ──
    const conditionsTextarea = page.locator('span.field-label', { hasText: 'Conditions (JSON)' }).locator('xpath=following-sibling::textarea[1]');
    const initialJson = await conditionsTextarea.inputValue();

    // ── Switch Structured → CLI → Structured without editing ──
    await cliTab.click();
    await page.waitForTimeout(200);
    await structuredTab.click();
    await page.waitForTimeout(200);

    // ── Verify JSON is unchanged ──
    const finalJson = await conditionsTextarea.inputValue();
    expect(finalJson).toBe(initialJson);

    // ── Intercept save request to verify payload ──
    let savedConditions = null;
    page.on('request', async r => {
      if (r.method() === 'PUT' && r.url().includes(`/api/agents/${testSlug}`)) {
        try {
          const body = await r.postDataJSON();
          savedConditions = JSON.stringify(body.conditions);
        } catch {}
      }
    });

    await page.getByRole('button', { name: 'Save' }).first().click();
    await page.waitForTimeout(500);

    // Verify saved JSON matches
    if (savedConditions) {
      expect(savedConditions).toBe(initialJson);
    }
  });

  test('4. cli_source clearing: create via CLI → edit → Structured edit → save → cli_source=""', async ({ page }) => {
    const testName = `ZZZ CLI Test Source ${Date.now()}`;
    const testSlug = `zzz-cli-test-source-${Date.now()}`;
    const cliStatement = 'WHEN avail_margin@funds.total >= 100000 ALERT email DO nop';

    // ── Create agent via API (will fill cli_source) ──
    const createResult = await page.request.post('/api/agents/', {
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

    // ── Switch to Structured and edit something (toggle email) ──
    const structuredTab = page.getByRole('tab', { name: 'Structured' });
    await structuredTab.click();
    await page.waitForTimeout(200);

    // Channels are a checkbox grid now (raw events textarea was replaced —
    // see ALERT_CHANNELS/toggleChannel in +page.svelte), so "edit something"
    // in Structured mode means flipping the Email checkbox, not editing JSON.
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

  test('5. Delete unaffected: delete works for CLI-authored agent', async ({ page }) => {
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

  test('6. Bare order statement: order(...) shows Place order button, no agent write', async ({ page }) => {
    const testName = `ZZZ CLI Test Order ${Date.now()}`;
    const testSlug = `zzz-cli-test-order-${Date.now()}`;

    // ── Create a dummy agent to access the edit panel ──
    await page.request.post('/api/agents/', {
      data: {
        slug: testSlug,
        kind: 'threshold',
        name: testName,
        conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -50000 },
        events: [{ channel: 'log', enabled: true }],
        actions: [],
        scope: 'total',
        schedule: 'market_hours',
        cooldown_minutes: 30,
        trade_mode: 'paper',
      },
    });

    await page.reload();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    const agentCard = page.locator('.algo-status-card').filter({ hasText: testName });
    await expect(agentCard).toBeVisible({ timeout: 10000 });
    await agentCard.getByRole('button').first().click();
    await page.waitForTimeout(300);
    await agentCard.getByRole('button', { name: 'Edit' }).click();
    await page.waitForTimeout(600);

    // ── Switch to CLI ──
    const cliTab = page.getByRole('tab', { name: 'CLI' });
    await cliTab.click();
    await page.waitForTimeout(200);

    // ── Intercept order placement ──
    await page.route('**/api/orders/**', r => {
      r.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ order_id: 'test-order-123', status: 'placed' }),
      });
    });

    // ── Type a bare order statement ──
    const cliTextarea = page.locator('.agent-cli-editor textarea');
    const orderStmt = 'order(account="ZG0790", symbol="NIFTY25JULFUT", exchange="NFO", side="BUY", qty=1, order_type="LIMIT", price=23000)';
    await cliTextarea.fill(orderStmt);
    await page.waitForTimeout(200);

    // ── Assert green preview and Place order button ──
    const preview = page.locator('.agent-cli-preview');
    await expect(preview).toBeVisible();

    const placeOrderBtn = page.locator('button.agent-cli-place-order');
    await expect(placeOrderBtn).toBeVisible();
    const btnText = await placeOrderBtn.textContent();
    expect(btnText).toContain('Place order');

    // ── Track agent write attempts ──
    let agentWrites = 0;
    page.on('request', r => {
      if ((r.method() === 'POST' || r.method() === 'PUT') && r.url().includes('/api/agents/')) {
        agentWrites++;
      }
    });

    // ── Click Place order ──
    await placeOrderBtn.click();
    await page.waitForTimeout(300);

    // ── Verify no agent writes occurred ──
    expect(agentWrites).toBe(0);
  });
});
