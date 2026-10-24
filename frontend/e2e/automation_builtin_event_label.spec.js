/**
 * automation_builtin_event_label.spec.js
 *
 * `AgentInfo.is_builtin_event` (2026-10) is a read-only, computed-at-
 * serialization field — True only for one of the 23 code-seeded
 * `kind='event'` agents in `event_agents.SEEDED_AGENTS`. Deliberately NOT
 * `Agent.is_system` (confirmed on dev: all 23 seeded event-kind rows carry
 * `is_system=False`) — see `_is_builtin_seeded_event_agent`'s docstring in
 * `backend/api/routes/agents.py`.
 *
 * The Automation page's expanded agent row renders a "Built-in" chip
 * (reusing the existing `.topic-badge` pattern already used for
 * `tpl.is_system` on the Brackets card) when `agent.is_builtin_event` is
 * true, and renders nothing for an operator-created row.
 *
 * Covers:
 *   1. A seeded-event row (`is_builtin_event: true`) shows the "Built-in"
 *      chip in the expanded row.
 *   2. An operator-authored `kind='event'` row (`is_builtin_event: false`)
 *      shows NO "Built-in" chip.
 *   3. A `kind='cycle'` built-in row with `is_system: true` but
 *      `is_builtin_event: false` (the real backend shape for e.g.
 *      `loss-funds-negative`) shows NO "Built-in" chip — this field is
 *      scoped to the seeded event-agent set only, not a general
 *      `is_system` passthrough.
 *
 * /api/agents/ is mocked, same precedent as
 * automation_pinned_event_agent_suffix.spec.js.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;

const BASE_FIELDS = {
  long_name: null, description: '', actions: [], scope: 'total',
  schedule: 'market_hours', cooldown_minutes: 30, fire_at_time: null,
  status: 'active', last_triggered_at: null, trigger_count: 0, last_error: null,
  lifespan_type: 'persistent', lifespan_max_fires: null,
  lifespan_expires_at: null, debounce_minutes: 0, tags: [], blackout_windows: [],
  trade_mode: 'paper', tier: 'medium', topic: 'general', group: null, cli_source: null,
  conditions: { log: { tag: 'error', min_level: 'ERROR' } }, resolved_channels: [],
};

// A real seeded-event slug (fill-alert) with the real backend shape:
// is_system=False, is_builtin_event=True.
const SEEDED_EVENT_ROW = {
  ...BASE_FIELDS, id: 501, slug: 'fill-alert', name: 'Seeded Event Agent E2E',
  kind: 'event', events: [], is_system: false, is_builtin_event: true,
  resolved_channels: [{ channel: 'telegram', enabled: true }],
};

// Operator-authored kind='event' row — is_builtin_event is False.
const CUSTOM_EVENT_ROW = {
  ...BASE_FIELDS, id: 502, slug: 'my-custom-alert', name: 'Custom Event Agent E2E',
  kind: 'event', events: [{ channel: 'telegram', enabled: true }],
  is_system: false, is_builtin_event: false,
};

// kind='cycle' BUILTIN_AGENTS row — is_system=True but is_builtin_event
// must stay False (disjoint signal, not a passthrough).
const CYCLE_BUILTIN_ROW = {
  ...BASE_FIELDS, id: 503, slug: 'loss-funds-negative-e2e', name: 'Cycle Builtin Agent E2E',
  kind: 'cycle', events: [{ channel: 'telegram', enabled: true }],
  is_system: true, is_builtin_event: false,
  conditions: { metric: 'cash', scope: 'funds.total', op: '<', value: 0 },
};

async function mockAgents(/** @type {import('@playwright/test').Page} */ page) {
  await page.route('**/api/agents/', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify([SEEDED_EVENT_ROW, CUSTOM_EVENT_ROW, CYCLE_BUILTIN_ROW]),
    });
  });
}

async function expandRow(/** @type {import('@playwright/test').Page} */ page, /** @type {string} */ cardId, /** @type {string} */ name) {
  await page.goto('/automation', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.kind-card-grid')).toBeVisible({ timeout: TIMEOUT });
  await page.locator(`.kind-card[data-kind="${cardId}"]`).click();
  const row = page.locator('[role="listitem"]').filter({ hasText: name });
  await expect(row).toBeVisible({ timeout: TIMEOUT });
  await row.locator('[role="button"]').first().click();
  await expect(row.getByText('Last fire:')).toBeVisible({ timeout: TIMEOUT });
  return row;
}

test.beforeEach(async ({ page }) => {
  await loginAsAdmin(page);
  await mockAgents(page);
});

test.describe('Automation — "Built-in" label for seeded event agents', () => {
  test('a seeded event agent (is_builtin_event: true) shows the "Built-in" chip', async ({ page }) => {
    const row = await expandRow(page, 'alerts', 'Seeded Event Agent E2E');
    await expect(row.getByText('Built-in', { exact: true })).toBeVisible();
  });

  test('an operator-authored event agent (is_builtin_event: false) shows NO "Built-in" chip', async ({ page }) => {
    const row = await expandRow(page, 'alerts', 'Custom Event Agent E2E');
    await expect(row.getByText('Built-in', { exact: true })).toHaveCount(0);
  });

  test('a kind=cycle built-in (is_system: true, is_builtin_event: false) shows NO "Built-in" chip — scoped to seeded event agents only', async ({ page }) => {
    const row = await expandRow(page, 'agents', 'Cycle Builtin Agent E2E');
    await expect(row.getByText('Built-in', { exact: true })).toHaveCount(0);
  });
});

test.describe('Automation — Delete button hidden for seeded event agents (delete-gap fix)', () => {
  test('Delete button is ABSENT for a seeded event agent (is_builtin_event: true)', async ({ page }) => {
    const row = await expandRow(page, 'alerts', 'Seeded Event Agent E2E');
    await expect(row.getByRole('button', { name: 'Delete' })).toHaveCount(0);
    // Edit must still be offered — only Delete is gated.
    await expect(row.getByRole('button', { name: 'Edit' })).toBeVisible();
  });

  test('Delete button is PRESENT for an operator-created event agent (is_builtin_event: false)', async ({ page }) => {
    const row = await expandRow(page, 'alerts', 'Custom Event Agent E2E');
    await expect(row.getByRole('button', { name: 'Delete' })).toBeVisible();
  });
});

test.describe('Automation — "Built-in" label, mobile viewport', () => {
  test.use({ viewport: { width: 360, height: 740 } });

  test('the "Built-in" chip renders without overflow on a 360px viewport', async ({ page }) => {
    const row = await expandRow(page, 'alerts', 'Seeded Event Agent E2E');
    const chip = row.getByText('Built-in', { exact: true });
    await expect(chip).toBeVisible();
    const box = await chip.boundingBox();
    const viewportSize = page.viewportSize();
    expect(box).not.toBeNull();
    expect(box.x + box.width).toBeLessThanOrEqual(viewportSize.width);
  });
});
