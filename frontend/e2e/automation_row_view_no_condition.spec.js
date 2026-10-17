/**
 * automation_row_view_no_condition.spec.js
 *
 * Regression guard: the per-row expanded "view" body for Agents/Alerts rows
 * on /automation (the `agentRowView` snippet in
 * `frontend/src/routes/(algo)/automation/+page.svelte`) no longer renders
 * the WHEN-tree "Condition" section. The full condition text is still
 * reachable via the row's Edit button, which decompiles the live JSON into
 * the Console tab's CLI editor (WHEN / ALERT / DO).
 *
 * Covers:
 *   1. Expanded Agents row: no "Condition" label, no .preview-tree, no
 *      "no conditions" fallback — while description, Actions, Tags and the
 *      footer info (Last fire / Alert via) stay visible.
 *   2. Expanded Alerts row: same negative assertions.
 *   3. Edit smoke: Edit on the Agents row opens the Console tab and the CLI
 *      textarea still carries the real WHEN text (full detail reachable).
 *
 * /api/agents/ is mocked (`page.route`) so the spec is deterministic and
 * does not depend on seeded dev-DB agents — same precedent as
 * automation_kind_card_drilldown.spec.js / automation_edit_decompile_resave.spec.js.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;

const CYCLE_ROW = {
  id: 301, slug: 'row-view-cycle-e2e', name: 'Row View Cycle E2E', long_name: null,
  description: 'Blocks on heavy account-level drawdown',
  conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -50000 },
  events: [{ channel: 'telegram', enabled: true }],
  actions: [{ type: 'deactivate_agent', params: {} }],
  scope: 'total', schedule: 'market_hours', cooldown_minutes: 30, fire_at_time: null,
  status: 'active', last_triggered_at: null, trigger_count: 0, last_error: null,
  is_system: false, lifespan_type: 'persistent', lifespan_max_fires: null,
  lifespan_expires_at: null, debounce_minutes: 0, tags: ['row-view-tag-e2e'],
  blackout_windows: [], trade_mode: 'paper', kind: 'cycle', tier: 'medium', topic: 'general',
  group: null, cli_source: null,
};

const EVENT_ROW = {
  id: 302, slug: 'row-view-alert-e2e', name: 'Row View Alert E2E', long_name: null,
  description: 'Surfaces any ERROR log line',
  conditions: { log: { tag: 'error', min_level: 'ERROR' } },
  events: [{ channel: 'telegram', enabled: true }, { channel: 'ntfy', enabled: true }],
  actions: [{ type: 'render', render: 'error' }], scope: 'total', schedule: 'market_hours',
  cooldown_minutes: 30, fire_at_time: null, status: 'active', last_triggered_at: null,
  trigger_count: 0, last_error: null, is_system: false, lifespan_type: 'persistent',
  lifespan_max_fires: null, lifespan_expires_at: null, debounce_minutes: 0,
  tags: ['row-view-tag-e2e'], blackout_windows: [], trade_mode: 'paper', kind: 'event',
  tier: 'medium', topic: 'general', group: null, cli_source: null,
};

async function mockAgents(/** @type {import('@playwright/test').Page} */ page) {
  await page.route('**/api/agents/', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify([CYCLE_ROW, EVENT_ROW]),
    });
  });
}

async function expandRow(/** @type {import('@playwright/test').Page} */ page, /** @type {string} */ cardId, /** @type {string} */ name) {
  // domcontentloaded, not networkidle — the page's WS reconnect loop keeps
  // the network non-idle indefinitely.
  await page.goto('/automation', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.kind-card-grid')).toBeVisible({ timeout: TIMEOUT });
  await page.locator(`.kind-card[data-kind="${cardId}"]`).click();
  const row = page.locator('[role="listitem"]').filter({ hasText: name });
  await expect(row).toBeVisible({ timeout: TIMEOUT });
  await row.locator('[role="button"]').first().click();
  // Wait for the expanded body (the Actions/Tags/footer blocks) to render.
  await expect(row.getByText('Last fire:')).toBeVisible({ timeout: TIMEOUT });
  return row;
}

test.beforeEach(async ({ page }) => {
  await loginAsAdmin(page);
  await mockAgents(page);
});

test.describe('Automation row view — Condition section removed from expanded Agents/Alerts rows', () => {
  test('expanded Agents row shows no Condition/tree, but keeps description, Actions, Tags and footer', async ({ page }) => {
    const row = await expandRow(page, 'agents', 'Row View Cycle E2E');

    // Negative: the WHEN-tree section is gone from the expanded body.
    await expect(row.locator('.preview-section-label', { hasText: /^Condition$/ })).toHaveCount(0);
    await expect(row.locator('.preview-tree')).toHaveCount(0);
    await expect(row.getByText('no conditions', { exact: true })).toHaveCount(0);

    // Positive: everything else in the expanded body is still rendered.
    await expect(row.locator('div.italic', { hasText: 'Blocks on heavy account-level drawdown' })).toBeVisible();
    await expect(row.locator('.preview-section-label', { hasText: /^Actions$/ })).toBeVisible();
    await expect(row.locator('.preview-action-type', { hasText: 'deactivate_agent' })).toBeVisible();
    await expect(row.locator('.preview-section-label', { hasText: /^Tags$/ })).toBeVisible();
    await expect(row.locator('.topic-badge', { hasText: 'row-view-tag-e2e' })).toBeVisible();
    await expect(row.getByText('Alert via:', { exact: true })).toBeVisible();
    await expect(row.getByText('Last fire:')).toBeVisible();
  });

  test('expanded Alerts row shows no Condition/tree, but keeps description, Alert via and footer', async ({ page }) => {
    const row = await expandRow(page, 'alerts', 'Row View Alert E2E');

    await expect(row.locator('.preview-section-label', { hasText: /^Condition$/ })).toHaveCount(0);
    await expect(row.locator('.preview-tree')).toHaveCount(0);
    await expect(row.getByText('no conditions', { exact: true })).toHaveCount(0);

    await expect(row.locator('div.italic', { hasText: 'Surfaces any ERROR log line' })).toBeVisible();
    await expect(row.getByText('Alert via:', { exact: true })).toBeVisible();
    await expect(row.getByText('Last fire:')).toBeVisible();
  });

  test('Edit still opens the Console tab with the full WHEN text (condition detail reachable there)', async ({ page }) => {
    const row = await expandRow(page, 'agents', 'Row View Cycle E2E');
    await row.getByRole('button', { name: 'Edit' }).click();

    await expect(page.locator('.console-edit-banner')).toContainText('Row View Cycle E2E', { timeout: TIMEOUT });
    const cliTextarea = page.locator('.agent-cli-textarea');
    await expect(cliTextarea).toHaveValue(/^WHEN /, { timeout: TIMEOUT });
    const value = await cliTextarea.inputValue();
    expect(value).toContain('pnl@positions.total');
  });
});
