/**
 * automation_pinned_event_agent_suffix.spec.js
 *
 * .claude/PLAN.md Step 5 (routing-tree activation plan) — minimal,
 * no-new-element text fix on the Automation page. A `kind='event'` agent
 * whose own `events` list is non-empty is "pinned" off the shared alert
 * routing tree (manual edit, or pre-SEED_VERSION-3 state) rather than
 * resolving its channels live via `alert_routing.py`. The existing
 * "Alert via:" text and the "Notify:" icon-strip `title` both get a short
 * suffix appended — no new badge/icon/element — ONLY for that case.
 *
 * Covers:
 *   1. A `kind='event'` row with non-empty `events` (pinned) shows the
 *      " (pinned — not on routing tree)" suffix on both "Alert via:" text
 *      and the icon-strip title.
 *   2. A `kind='event'` row with EMPTY `events` and a non-empty
 *      `resolved_channels` (the real post-SEED_VERSION-3 shape every
 *      migrated seeded agent now has) shows the RESOLVED channel list,
 *      not "none"/empty — and no suffix. This is the regression case for
 *      the `resolved_channels` display fix (2026-10 routing-tree
 *      activation): `channelSummary`/`enabledChannels` must read
 *      `agent.resolved_channels`, not raw (now-empty) `agent.events`.
 *   3. A `kind='cycle'` row with non-empty `events` AND an empty
 *      `resolved_channels: []` (exactly what `_event_agent_resolved_channels`
 *      returns for any non-event-kind agent) still shows its OWN `events`
 *      — guards against a naive unconditional `resolved_channels ??
 *      events` swap blanking "Alert via:" for every cycle/threshold agent.
 *
 * /api/agents/ is mocked, same precedent as
 * automation_row_view_no_condition.spec.js.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;

const BASE_FIELDS = {
  long_name: null, description: '', actions: [], scope: 'total',
  schedule: 'market_hours', cooldown_minutes: 30, fire_at_time: null,
  status: 'active', last_triggered_at: null, trigger_count: 0, last_error: null,
  is_system: false, lifespan_type: 'persistent', lifespan_max_fires: null,
  lifespan_expires_at: null, debounce_minutes: 0, tags: [], blackout_windows: [],
  trade_mode: 'paper', tier: 'medium', topic: 'general', group: null, cli_source: null,
  conditions: { log: { tag: 'error', min_level: 'ERROR' } },
};

// Pinned: kind='event' with a non-empty raw `events` list — off the tree.
const PINNED_EVENT_ROW = {
  ...BASE_FIELDS, id: 401, slug: 'pinned-event-e2e', name: 'Pinned Event Agent E2E',
  kind: 'event', events: [{ channel: 'telegram', enabled: true }, { channel: 'ntfy', enabled: true }],
  resolved_channels: [{ channel: 'telegram', enabled: true }, { channel: 'ntfy', enabled: true }],
};

// Normal: kind='event' with an empty `events` list and a non-empty
// `resolved_channels` — tree-driven, the expected post-SEED_VERSION-3 state
// for every seeded agent. "Alert via:" / Notify must show the RESOLVED
// channels (ntfy, telegram), not "none"/empty.
const TREE_EVENT_ROW = {
  ...BASE_FIELDS, id: 402, slug: 'tree-event-e2e', name: 'Tree Event Agent E2E',
  kind: 'event', events: [],
  resolved_channels: [{ channel: 'ntfy', enabled: true }, { channel: 'telegram', enabled: true }],
};

// Ordinary cycle agent with its own (non-empty) events — not a routing-tree
// participant at all, so "pinned" must never apply to it. `resolved_channels:
// []` matches what `_event_agent_resolved_channels` really returns for any
// non-event-kind agent — the display logic must NOT let this empty list
// override the agent's own real `events`.
const CYCLE_ROW = {
  ...BASE_FIELDS, id: 403, slug: 'cycle-agent-e2e', name: 'Cycle Agent E2E',
  kind: 'cycle', events: [{ channel: 'telegram', enabled: true }], resolved_channels: [],
  conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -50000 },
};

async function mockAgents(/** @type {import('@playwright/test').Page} */ page) {
  await page.route('**/api/agents/', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify([PINNED_EVENT_ROW, TREE_EVENT_ROW, CYCLE_ROW]),
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

test.describe('Automation — pinned-event-agent "not on routing tree" text suffix', () => {
  test('pinned event agent (non-empty events) shows the suffix on "Alert via:" and the Notify icon-strip title', async ({ page }) => {
    const row = await expandRow(page, 'alerts', 'Pinned Event Agent E2E');

    const alertViaLine = row.locator('span', { hasText: 'Alert via:' }).locator('..');
    await expect(alertViaLine).toContainText('telegram, ntfy (pinned — not on routing tree)');

    const iconStrip = row.locator('.agent-row-icons');
    await expect(iconStrip).toHaveAttribute('title', /telegram, ntfy \(pinned — not on routing tree\)/);
  });

  test('tree-driven event agent (empty events, non-empty resolved_channels) shows the RESOLVED channels, no suffix', async ({ page }) => {
    const row = await expandRow(page, 'alerts', 'Tree Event Agent E2E');

    const alertViaLine = row.locator('span', { hasText: 'Alert via:' }).locator('..');
    await expect(alertViaLine).toContainText('ntfy, telegram');
    await expect(alertViaLine).not.toContainText('pinned');

    const iconStrip = row.locator('.agent-row-icons');
    await expect(iconStrip).toHaveAttribute('title', /Notify: ntfy, telegram/);
    const title = await iconStrip.getAttribute('title');
    expect(title).not.toContain('pinned');
    expect(title).not.toContain('none');
  });

  test('ordinary cycle agent with its own events (empty resolved_channels) shows its OWN channels, no suffix — "pinned" and resolved_channels are scoped to kind=event only', async ({ page }) => {
    const row = await expandRow(page, 'agents', 'Cycle Agent E2E');

    const alertViaLine = row.locator('span', { hasText: 'Alert via:' }).locator('..');
    await expect(alertViaLine).toContainText('telegram');
    await expect(alertViaLine).not.toContainText('pinned');

    const iconStrip = row.locator('.agent-row-icons');
    const title = await iconStrip.getAttribute('title');
    expect(title).toContain('telegram');
    expect(title).not.toContain('pinned');
    expect(title).not.toContain('none');
  });
});
