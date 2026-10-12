/**
 * automation_edit_decompile_resave.spec.js — Sprint C (2026-10, Phase 2
 * "Automation redesign").
 *
 * Edit on any row (any kind) decompiles its live JSON into the Console
 * tab's CLI text and arms edit mode, so Save routes through the real
 * UPDATE endpoint for that row — never the create-only path. This spec
 * proves the round-trip for the three Agent-table kinds (cycle/Agents,
 * event/Alerts, template/Templates); the Bracket kind's equivalent round
 * trip already lives in console_decompile_bracket_edit_flow.spec.js.
 *
 * Each endpoint is mocked (`page.route`) — the PUT/PATCH interceptor
 * asserts it was actually called with the right slug/id, which is the
 * one thing a toast alone can't prove.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;

const CYCLE_ROW = {
  id: 101, slug: 'loss-funds-negative-e2e', name: 'Loss Funds Negative E2E', long_name: null,
  description: '', conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -50000 },
  events: [{ channel: 'telegram', enabled: true }], actions: [], scope: 'total',
  schedule: 'market_hours', cooldown_minutes: 30, fire_at_time: null, status: 'active',
  last_triggered_at: null, trigger_count: 0, last_error: null, is_system: false,
  lifespan_type: 'persistent', lifespan_max_fires: null, lifespan_expires_at: null,
  debounce_minutes: 0, tags: [], blackout_windows: [], trade_mode: 'paper', kind: 'cycle',
  tier: 'medium', topic: 'general', group: null, cli_source: null,
};

const EVENT_ROW = {
  id: 102, slug: 'error-alert-e2e', name: 'Error Alert E2E', long_name: null, description: '',
  conditions: { log: { tag: 'error', min_level: 'ERROR' } },
  events: [{ channel: 'telegram', enabled: true }],
  actions: [{ type: 'render', render: 'error' }], scope: 'total', schedule: 'market_hours',
  cooldown_minutes: 30, fire_at_time: null, status: 'active', last_triggered_at: null,
  trigger_count: 0, last_error: null, is_system: false, lifespan_type: 'persistent',
  lifespan_max_fires: null, lifespan_expires_at: null, debounce_minutes: 0, tags: [],
  blackout_windows: [], trade_mode: 'paper', kind: 'event', tier: 'medium', topic: 'general',
  group: null, cli_source: null,
};

const TEMPLATE_ROW = {
  id: 103, slug: 'notify-critical-trio-e2e', name: 'Notify Critical Trio E2E', long_name: null,
  description: '', conditions: {}, events: [{ channel: 'telegram', enabled: true }], actions: [],
  scope: 'total', schedule: 'market_hours', cooldown_minutes: 30, fire_at_time: null,
  status: 'active', last_triggered_at: null, trigger_count: 0, last_error: null, is_system: false,
  lifespan_type: 'persistent', lifespan_max_fires: null, lifespan_expires_at: null,
  debounce_minutes: 0, tags: [], blackout_windows: [], trade_mode: 'paper', kind: 'template',
  tier: 'medium', topic: 'general', group: null, cli_source: null,
};

async function mockAgentsList(/** @type {import('@playwright/test').Page} */ page, /** @type {any[]} */ rows) {
  await page.route('**/api/agents/', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(rows) });
  });
}

async function openCardAndRow(/** @type {import('@playwright/test').Page} */ page, /** @type {string} */ cardId, /** @type {string} */ name) {
  // domcontentloaded, not networkidle — this page's WS reconnect loop
  // keeps the network non-idle indefinitely.
  await page.goto('/automation', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.kind-card-grid')).toBeVisible({ timeout: TIMEOUT });
  await page.locator(`.kind-card[data-kind="${cardId}"]`).click();
  const row = page.locator('[role="listitem"]').filter({ hasText: name });
  await expect(row).toBeVisible({ timeout: TIMEOUT });
  // Expand the row (click its own clickable header) so the Edit/Delete
  // footer actually renders.
  await row.locator('[role="button"]').first().click();
  return row;
}

test.beforeEach(async ({ page }) => {
  await loginAsAdmin(page);
});

test('cycle Agent: Edit decompiles, Save PUTs /api/agents/{slug} — never POSTs', async ({ page }) => {
  let postCalled = false;
  // ONE handler for the collection endpoint, discriminating by method —
  // a second `page.route` for the same glob pattern would shadow this
  // one (Playwright dispatches to the MOST RECENTLY registered matching
  // handler only).
  await page.route('**/api/agents/', async (route) => {
    if (route.request().method() === 'POST') { postCalled = true; await route.continue(); return; }
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([CYCLE_ROW]) });
  });
  let putCalled = /** @type {any} */ (null);
  await page.route('**/api/agents/loss-funds-negative-e2e', async (route) => {
    if (route.request().method() !== 'PUT') { await route.continue(); return; }
    putCalled = { url: route.request().url(), body: route.request().postDataJSON() };
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ detail: 'ok' }) });
  });

  const row = await openCardAndRow(page, 'agents', 'Loss Funds Negative E2E');
  await row.getByRole('button', { name: 'Edit' }).click();

  await expect(page.locator('.console-edit-banner')).toContainText('Loss Funds Negative E2E', { timeout: TIMEOUT });
  const cliTextarea = page.locator('.agent-cli-textarea');
  const value = await cliTextarea.inputValue();
  expect(value).toMatch(/^WHEN /);
  expect(value).toContain('pnl@positions.total');

  const saveBtn = page.locator('.console-agent-cli-btn');
  await expect(saveBtn).toHaveText('Save', { timeout: TIMEOUT });
  await saveBtn.click();

  await expect.poll(() => putCalled, { timeout: TIMEOUT }).not.toBeNull();
  expect(putCalled.url).toContain('/api/agents/loss-funds-negative-e2e');
  expect(postCalled).toBe(false);
  await expect(page.locator('.console-edit-banner')).toHaveCount(0);
});

test('event Agent (Alert): Edit decompiles, Save PUTs the same slug', async ({ page }) => {
  await mockAgentsList(page, [EVENT_ROW]);
  let putCalled = /** @type {any} */ (null);
  await page.route('**/api/agents/error-alert-e2e', async (route) => {
    if (route.request().method() !== 'PUT') { await route.continue(); return; }
    putCalled = { url: route.request().url(), body: route.request().postDataJSON() };
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ detail: 'ok' }) });
  });

  const row = await openCardAndRow(page, 'alerts', 'Error Alert E2E');
  await row.getByRole('button', { name: 'Edit' }).click();

  await expect(page.locator('.console-edit-banner')).toContainText('Error Alert E2E', { timeout: TIMEOUT });
  const cliTextarea = page.locator('.agent-cli-textarea');
  const value = await cliTextarea.inputValue();
  expect(value).toContain('log_tag=error');

  const saveBtn = page.locator('.console-agent-cli-btn');
  await expect(saveBtn).toHaveText('Save', { timeout: TIMEOUT });
  await saveBtn.click();

  await expect.poll(() => putCalled, { timeout: TIMEOUT }).not.toBeNull();
  expect(putCalled.url).toContain('/api/agents/error-alert-e2e');
});

test('Template (Token): Edit decompiles, Save PUTs /api/agents/{slug} — never the retired fragments route', async ({ page }) => {
  await mockAgentsList(page, [TEMPLATE_ROW]);
  let putCalled = /** @type {any} */ (null);
  let fragmentsRouteCalled = false;
  await page.route('**/api/admin/fragments/**', async (route) => {
    fragmentsRouteCalled = true;
    await route.continue();
  });
  await page.route('**/api/agents/notify-critical-trio-e2e', async (route) => {
    if (route.request().method() !== 'PUT') { await route.continue(); return; }
    putCalled = { url: route.request().url(), body: route.request().postDataJSON() };
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ detail: 'ok' }) });
  });

  // 'templates' card no longer exists (2026-10-12 — Templates folded
  // into Agents); this row now lives under 'agents'.
  const row = await openCardAndRow(page, 'agents', 'Notify Critical Trio E2E');
  await row.getByRole('button', { name: 'Edit' }).click();

  await expect(page.locator('.console-edit-banner')).toContainText('Notify Critical Trio E2E', { timeout: TIMEOUT });
  const cliTextarea = page.locator('.agent-cli-textarea');
  const value = await cliTextarea.inputValue();
  expect(value).toMatch(/^token\(kind=notify\)/);

  const saveBtn = page.locator('.console-agent-cli-btn');
  await expect(saveBtn).toHaveText('Save', { timeout: TIMEOUT });
  await saveBtn.click();

  await expect.poll(() => putCalled, { timeout: TIMEOUT }).not.toBeNull();
  expect(putCalled.url).toContain('/api/agents/notify-critical-trio-e2e');
  expect(putCalled.body.events).toEqual([{ channel: 'telegram', enabled: true }]);
  expect(fragmentsRouteCalled).toBe(false);
});
