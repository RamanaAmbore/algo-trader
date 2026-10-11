/**
 * automation_kind_card_drilldown.spec.js — Sprint C (2026-10, Phase 2
 * "Automation redesign").
 *
 * Covers the new kind-count-card → per-kind #group= tabs → row-list
 * drill-down that replaces the old flat Agents grid + content-based
 * categorization on `/automation`'s Agents tab:
 *   1. The four kind-cards (Agents/Alerts/Brackets/Templates) show the
 *      right row counts and drilling into each shows the right rows.
 *   2. Groups are strictly per-kind — four rows (one Agent-cycle, one
 *      Agent-event, one Bracket, one Template) all share the SAME
 *      `group` string ("hedge-book"); each kind's own "hedge-book" tab
 *      must show EXACTLY its own row, never another kind's.
 *
 * All four list endpoints are mocked (`page.route`) so this spec is
 * deterministic regardless of real dev-DB contents. The grammar-tokens
 * catalog fetch is real network (same convention as the sibling Console
 * specs — `view_agents_catalog` already covers the default admin role).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;

const MANUAL_ROW = {
  id: 1, slug: 'manual', name: 'Manual', long_name: null, description: '',
  conditions: null, events: [], actions: [], scope: 'total', schedule: 'market_hours',
  cooldown_minutes: 30, fire_at_time: null, status: 'inactive', last_triggered_at: null,
  trigger_count: 0, last_error: null, is_system: true, lifespan_type: 'persistent',
  lifespan_max_fires: null, lifespan_expires_at: null, debounce_minutes: 0, tags: [],
  blackout_windows: [], trade_mode: 'paper', kind: 'cycle', tier: 'medium', topic: 'general',
  group: null, cli_source: null,
};

const CYCLE_ROW = {
  id: 101, slug: 'loss-funds-negative-e2e', name: 'Loss Funds Negative E2E', long_name: null,
  description: '', conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -50000 },
  events: [{ channel: 'telegram', enabled: true }], actions: [], scope: 'total',
  schedule: 'market_hours', cooldown_minutes: 30, fire_at_time: null, status: 'active',
  last_triggered_at: null, trigger_count: 0, last_error: null, is_system: false,
  lifespan_type: 'persistent', lifespan_max_fires: null, lifespan_expires_at: null,
  debounce_minutes: 0, tags: [], blackout_windows: [], trade_mode: 'paper', kind: 'cycle',
  tier: 'medium', topic: 'general', group: 'hedge-book', cli_source: null,
};

const EVENT_ROW = {
  id: 102, slug: 'error-alert-e2e', name: 'Error Alert E2E', long_name: null, description: '',
  conditions: { log: { tag: 'error', min_level: 'ERROR' } },
  events: [{ channel: 'telegram', enabled: true }, { channel: 'ntfy', enabled: true }],
  actions: [{ type: 'render', render: 'error' }], scope: 'total', schedule: 'market_hours',
  cooldown_minutes: 30, fire_at_time: null, status: 'active', last_triggered_at: null,
  trigger_count: 0, last_error: null, is_system: false, lifespan_type: 'persistent',
  lifespan_max_fires: null, lifespan_expires_at: null, debounce_minutes: 0, tags: [],
  blackout_windows: [], trade_mode: 'paper', kind: 'event', tier: 'medium', topic: 'general',
  group: 'hedge-book', cli_source: null,
};

const TEMPLATE_ROW = {
  id: 103, slug: 'notify-critical-trio-e2e', name: 'Notify Critical Trio E2E', long_name: null,
  description: '', conditions: {}, events: [{ channel: 'telegram', enabled: true }], actions: [],
  scope: 'total', schedule: 'market_hours', cooldown_minutes: 30, fire_at_time: null,
  status: 'active', last_triggered_at: null, trigger_count: 0, last_error: null, is_system: false,
  lifespan_type: 'persistent', lifespan_max_fires: null, lifespan_expires_at: null,
  debounce_minutes: 0, tags: [], blackout_windows: [], trade_mode: 'paper', kind: 'template',
  tier: 'medium', topic: 'general', group: 'hedge-book', cli_source: null,
};

const AGENT_ROWS = [MANUAL_ROW, CYCLE_ROW, EVENT_ROW, TEMPLATE_ROW];

const BRACKET_ROW = {
  id: 201, slug: null, name: 'E2E Bracket', description: '', applies_to: 'both',
  tp_pct: 30, sl_pct: -20, wing_premium_pct: null, wing_strike_offset: null,
  wing_max_spread_pct: null, tp_order_type: 'LIMIT', tp_scales_json: null, sl_trail_pct: null,
  is_default: false, is_system: false, is_active: true, group: 'hedge-book', tags: [],
};

async function mockLists(/** @type {import('@playwright/test').Page} */ page) {
  await page.route('**/api/agents/', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(AGENT_ROWS) });
  });
  await page.route('**/api/admin/templates/', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([BRACKET_ROW]) });
  });
}

test.beforeEach(async ({ page }) => {
  await loginAsAdmin(page);
  await mockLists(page);
});

test.describe('Agents tab — kind-count cards (Sprint C)', () => {
  test('card counts exclude the manual row and split agents/alerts/templates correctly', async ({ page }) => {
    await page.goto('/automation');
    await page.waitForLoadState('networkidle');

    await expect(page.locator('.kind-card[data-kind="agents"] .kind-card-n')).toHaveText('1', { timeout: TIMEOUT });
    await expect(page.locator('.kind-card[data-kind="alerts"] .kind-card-n')).toHaveText('1');
    await expect(page.locator('.kind-card[data-kind="brackets"] .kind-card-n')).toHaveText('1');
    await expect(page.locator('.kind-card[data-kind="templates"] .kind-card-n')).toHaveText('1');
  });

  test('clicking a card drills into its own group tabs and row list', async ({ page }) => {
    await page.goto('/automation');
    await page.waitForLoadState('networkidle');

    await page.locator('.kind-card[data-kind="agents"]').click();
    await expect(page.locator('h2.section-heading', { hasText: 'Agents' })).toBeVisible({ timeout: TIMEOUT });
    await expect(page.getByRole('tab', { name: /hedge-book/ })).toBeVisible({ timeout: TIMEOUT });
    await expect(page.locator('[role="listitem"]').filter({ hasText: 'Loss Funds Negative E2E' })).toBeVisible();
    // The event-kind row must NOT appear under the Agents card.
    await expect(page.locator('[role="listitem"]').filter({ hasText: 'Error Alert E2E' })).toHaveCount(0);

    await page.locator('button', { hasText: 'Back to cards' }).click();
    await expect(page.locator('.kind-card-grid')).toBeVisible({ timeout: TIMEOUT });
  });
});

test.describe('Agents tab — groups are strictly per-kind (Sprint C regression)', () => {
  test('four rows of four different kinds share the SAME group name — each kind\'s own tab shows only its own row', async ({ page }) => {
    await page.goto('/automation');
    await page.waitForLoadState('networkidle');

    const cases = [
      { card: 'agents',    expectRow: 'Loss Funds Negative E2E', excludeRows: ['Error Alert E2E', 'Notify Critical Trio E2E', 'E2E Bracket'] },
      { card: 'alerts',    expectRow: 'Error Alert E2E',          excludeRows: ['Loss Funds Negative E2E', 'Notify Critical Trio E2E', 'E2E Bracket'] },
      { card: 'brackets',  expectRow: 'E2E Bracket',              excludeRows: ['Loss Funds Negative E2E', 'Error Alert E2E', 'Notify Critical Trio E2E'] },
      { card: 'templates', expectRow: 'Notify Critical Trio E2E', excludeRows: ['Loss Funds Negative E2E', 'Error Alert E2E', 'E2E Bracket'] },
    ];

    for (const c of cases) {
      await page.locator('.kind-card[data-kind="' + c.card + '"]').click();
      const groupTab = page.getByRole('tab', { name: /hedge-book/ });
      await expect(groupTab, `${c.card} card must show a "hedge-book" group tab`).toBeVisible({ timeout: TIMEOUT });
      await groupTab.click();
      await expect(page.locator('[role="listitem"]').filter({ hasText: c.expectRow }), `${c.card} hedge-book tab must show ${c.expectRow}`)
        .toHaveCount(1, { timeout: TIMEOUT });
      for (const excluded of c.excludeRows) {
        await expect(page.locator('[role="listitem"]').filter({ hasText: excluded }), `${c.card} hedge-book tab must NOT show ${excluded}`)
          .toHaveCount(0);
      }
      await page.locator('button', { hasText: 'Back to cards' }).click();
    }
  });
});
