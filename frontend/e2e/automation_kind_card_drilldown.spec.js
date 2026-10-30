/**
 * automation_kind_card_drilldown.spec.js — Sprint C (2026-10, Phase 2
 * "Automation redesign"), REVISED 2026-10-12 per operator instruction
 * ("use order status cards as reference... keep agents status cards
 * always, and show the agent rows below when a status card pressed...
 * toggle... instead of using go back... show the group tag in the row,
 * sort the rows on group tag").
 *
 * Covers the kind-count-card → row-list toggle-filter (matching
 * OrderBook.svelte's `.ob-status-bar`/`.ob-sc` reference model exactly:
 * cards always visible, exactly one active, no Back button) that
 * replaces the old flat Agents grid AND the original Sprint C per-kind
 * `#group=` tab layer (now gone — each row shows its own `group` as an
 * inline tag instead, rows sorted by group):
 *   1. The four kind-cards (Agents/Alerts/Brackets/Templates) show the
 *      right row counts and selecting each shows the right rows,
 *      directly below the cards, with no navigation.
 *   2. Groups are strictly per-kind — four rows (one Agent-cycle, one
 *      Agent-event, one Bracket, one Template) all share the SAME
 *      `group` string ("hedge-book"); each kind's own row list must show
 *      EXACTLY its own row (never another kind's), tagged "hedge-book".
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
  description: 'Blocks on heavy account-level drawdown', conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -50000 },
  events: [{ channel: 'telegram', enabled: true }], actions: [], scope: 'total',
  schedule: 'market_hours', cooldown_minutes: 30, fire_at_time: null, status: 'active',
  last_triggered_at: null, trigger_count: 0, last_error: null, is_system: false,
  lifespan_type: 'persistent', lifespan_max_fires: null, lifespan_expires_at: null,
  debounce_minutes: 0, tags: [], blackout_windows: [], trade_mode: 'paper', kind: 'cycle',
  tier: 'medium', topic: 'general', group: 'hedge-book', cli_source: null,
};

// Second 'agents'-kind row, deliberately UNGROUPED (group: null) — proves
// (1) the fallback "Ungrouped" tag renders, and (2) sort-by-group puts
// the named "hedge-book" group before the ungrouped bucket, matching
// `groupRowsByGroup`'s own documented convention (named groups
// alphabetically first, ungrouped bucket always last).
const CYCLE_ROW_UNGROUPED = {
  id: 104, slug: 'aaa-ungrouped-e2e', name: 'AAA Ungrouped E2E', long_name: null,
  description: '', conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -1 },
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

// Sprint E (Agent/Bracket/Template unification phase) — the Brackets
// card now reads `GET /api/agents/` too (every `kind='bracket'` row,
// reshaped client-side via `bracketAgentToTemplateOut`), NOT
// `GET /api/admin/templates/` directly — this is now a REAL Agent row
// riding along in `AGENT_ROWS`, not a separately-mocked endpoint.
// `legacy_template_id` (201) is the canonical `OrderTemplate.id` this
// row mirrors; `id` (202, deliberately DIFFERENT) is this Agent row's
// own, unrelated primary key — proving the UI reads `legacy_template_id`
// through the adapter, not `id`, matches the backend council-risk
// review's fix #2 on the frontend side.
const BRACKET_AGENT_ROW = {
  id: 202, slug: 'bracket-201', name: 'E2E Bracket', long_name: null, description: '',
  conditions: {}, events: [], actions: [], scope: 'total', schedule: 'market_hours',
  cooldown_minutes: 30, fire_at_time: null, status: 'active', last_triggered_at: null,
  trigger_count: 0, last_error: null, is_system: false, lifespan_type: 'persistent',
  lifespan_max_fires: null, lifespan_expires_at: null, debounce_minutes: 0, tags: [],
  blackout_windows: [], trade_mode: 'paper', kind: 'bracket', tier: 'medium', topic: 'general',
  group: 'hedge-book', cli_source: null,
  legacy_template_id: 201,
  bracket_config: {
    applies_to: 'both', tp_pct: 30, sl_pct: -20, wing_premium_pct: null,
    wing_strike_offset: null, wing_max_spread_pct: null, tp_order_type: 'LIMIT',
    tp_scales_json: null, sl_trail_pct: null, is_default: false,
  },
};

const AGENT_ROWS = [MANUAL_ROW, CYCLE_ROW, CYCLE_ROW_UNGROUPED, EVENT_ROW, TEMPLATE_ROW, BRACKET_AGENT_ROW];

async function mockLists(/** @type {import('@playwright/test').Page} */ page) {
  await page.route('**/api/agents/', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(AGENT_ROWS) });
  });
}

test.beforeEach(async ({ page }) => {
  await loginAsAdmin(page);
  await mockLists(page);
});

test.describe('Agents tab — kind-count cards (Sprint C)', () => {
  test('card counts exclude the manual row and split agents/alerts/brackets correctly (Templates folded into Agents, 2026-10-12)', async ({ page }) => {
    // domcontentloaded, not networkidle — this page's WS reconnect loop
    // keeps the network non-idle indefinitely (same caveat already
    // documented in automation_agent_row_card_styling.spec.js).
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.kind-card-grid')).toBeVisible({ timeout: TIMEOUT });

    // CYCLE_ROW + CYCLE_ROW_UNGROUPED + TEMPLATE_ROW (kind=template, now
    // folded into Agents) = 3.
    await expect(page.locator('.kind-card[data-kind="agents"] .kind-card-n')).toHaveText('3', { timeout: TIMEOUT });
    await expect(page.locator('.kind-card[data-kind="alerts"] .kind-card-n')).toHaveText('1');
    await expect(page.locator('.kind-card[data-kind="brackets"] .kind-card-n')).toHaveText('1');
    await expect(page.locator('.kind-card[data-kind="templates"]')).toHaveCount(0);
  });

  // 2026-10-13 regression (operator: declutter the collapsed row) — the
  // trade-mode L/P toggle and ON/OFF toggle are action/detail controls
  // that must stay hidden until the row is expanded, not clutter the
  // always-visible collapsed row.
  test('collapsed row hides the trade-mode/ON-OFF buttons; expanding the row reveals them', async ({ page }) => {
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.kind-card-grid')).toBeVisible({ timeout: TIMEOUT });

    const cycleRow = page.locator('[role="listitem"]').filter({ hasText: 'Loss Funds Negative E2E' });
    await expect(cycleRow).toBeVisible({ timeout: TIMEOUT });

    // Collapsed: identity-only (status dot, name, log-chip, chevron) —
    // no trade-mode toggle, no ON/OFF toggle.
    await expect(cycleRow.locator('button[title*="Trade mode" i]')).toHaveCount(0);
    await expect(cycleRow.getByRole('button', { name: 'ON', exact: true })).toHaveCount(0);
    await expect(cycleRow.getByRole('button', { name: 'OFF', exact: true })).toHaveCount(0);

    // Expand — both controls are now reachable (CYCLE_ROW is status:
    // 'active', so its toggle reads "ON").
    await cycleRow.locator('div[role="button"]').first().click();
    await expect(cycleRow.locator('button[title*="Trade mode" i]')).toBeVisible({ timeout: TIMEOUT });
    await expect(cycleRow.getByRole('button', { name: 'ON', exact: true })).toBeVisible();
  });

  test('clicking a card toggles the row list below it — cards stay visible, no navigation', async ({ page }) => {
    // domcontentloaded, not networkidle — this page's WS reconnect loop
    // keeps the network non-idle indefinitely (same caveat already
    // documented in automation_agent_row_card_styling.spec.js).
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.kind-card-grid')).toBeVisible({ timeout: TIMEOUT });

    // Default-active card is 'agents' — its rows are already visible with
    // no click needed (matches OrderBook.svelte's always-one-active model).
    await expect(page.locator('.kind-card-grid')).toBeVisible({ timeout: TIMEOUT });
    await expect(page.locator('[role="listitem"]').filter({ hasText: 'Loss Funds Negative E2E' })).toBeVisible({ timeout: TIMEOUT });

    await page.locator('.kind-card[data-kind="agents"]').click();
    // Cards remain visible (never replaced by a drilled-down screen) and
    // there is no Back/navigation affordance to return from.
    await expect(page.locator('.kind-card-grid')).toBeVisible();
    await expect(page.locator('button', { hasText: 'Back to cards' })).toHaveCount(0);
    await expect(page.locator('[role="listitem"]').filter({ hasText: 'Loss Funds Negative E2E' })).toBeVisible();
    // The event-kind row must NOT appear under the Agents card.
    await expect(page.locator('[role="listitem"]').filter({ hasText: 'Error Alert E2E' })).toHaveCount(0);

    // Collapsed row shows name + group + description without expanding
    // (operator: "make agent row card styling similar to order card
    // styling... name, group and short description"). Originally reused
    // `.log-chip` (OrderCard.svelte's chip row); 2026-10-29 (operator:
    // "remove background and border off-white agent description text")
    // swapped to the dedicated `.aw-row-desc` plain-text class — `.log-chip`
    // itself is untouched for its other, genuinely chip-like usages.
    const cycleRow = page.locator('[role="listitem"]').filter({ hasText: 'Loss Funds Negative E2E' });
    await expect(cycleRow.locator('.aw-row-desc')).toContainText('Blocks on heavy account-level drawdown');

    // Switching to a different kind card changes the row list IN PLACE.
    await page.locator('.kind-card[data-kind="alerts"]').click();
    await expect(page.locator('.kind-card-grid')).toBeVisible();
    await expect(page.locator('[role="listitem"]').filter({ hasText: 'Error Alert E2E' })).toBeVisible({ timeout: TIMEOUT });
    await expect(page.locator('[role="listitem"]').filter({ hasText: 'Loss Funds Negative E2E' })).toHaveCount(0);
  });

  test('the active kind card gets a visibly different state than an inactive one', async ({ page }) => {
    // domcontentloaded, not networkidle — this page's WS reconnect loop
    // keeps the network non-idle indefinitely (same caveat already
    // documented in automation_agent_row_card_styling.spec.js).
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.kind-card-grid')).toBeVisible({ timeout: TIMEOUT });

    await page.locator('.kind-card[data-kind="alerts"]').click();
    const active = page.locator('.kind-card[data-kind="alerts"]');
    const inactive = page.locator('.kind-card[data-kind="agents"]');
    await expect(active).toHaveClass(/is-active/);
    await expect(inactive).not.toHaveClass(/is-active/);
  });
});

test.describe('Agents tab — groups are strictly per-kind (Sprint C regression), group tag shown + sorted (2026-10-12 revision)', () => {
  test('four rows of four different kinds share the SAME group name — each kind\'s own row list shows only its own row, tagged "hedge-book"', async ({ page }) => {
    // domcontentloaded, not networkidle — this page's WS reconnect loop
    // keeps the network non-idle indefinitely (same caveat already
    // documented in automation_agent_row_card_styling.spec.js).
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.kind-card-grid')).toBeVisible({ timeout: TIMEOUT });

    // 'Notify Critical Trio E2E' (kind=template) now shares the 'agents'
    // card with 'Loss Funds Negative E2E' (kind=cycle) — 2026-10-12,
    // Templates folded into Agents — so the 'agents' case expects BOTH
    // rows instead of excluding one.
    const cases = [
      { card: 'agents',   expectRows: ['Loss Funds Negative E2E', 'Notify Critical Trio E2E'], excludeRows: ['Error Alert E2E', 'E2E Bracket'] },
      { card: 'alerts',   expectRows: ['Error Alert E2E'],                                      excludeRows: ['Loss Funds Negative E2E', 'Notify Critical Trio E2E', 'E2E Bracket'] },
      { card: 'brackets', expectRows: ['E2E Bracket'],                                          excludeRows: ['Loss Funds Negative E2E', 'Error Alert E2E', 'Notify Critical Trio E2E'] },
    ];

    for (const c of cases) {
      await page.locator('.kind-card[data-kind="' + c.card + '"]').click();
      for (const expectName of c.expectRows) {
        const row = page.locator('[role="listitem"]').filter({ hasText: expectName });
        await expect(row, `${c.card} card must show ${expectName}`).toHaveCount(1, { timeout: TIMEOUT });
        // Group now folds into the single description .aw-row-desc as a
        // trailing `#group:name` tag (2026-10-12 revision) rather than
        // its own separate chip — operator: "instead of having a
        // separate group button... put it in the description, as
        // #group: at the end of the description."
        await expect(row.locator('.aw-row-desc'), `${expectName}'s row must show its group as a #group: tag`)
          .toContainText('#group:hedge-book');
      }
      for (const excluded of c.excludeRows) {
        await expect(page.locator('[role="listitem"]').filter({ hasText: excluded }), `${c.card} must NOT show ${excluded}`)
          .toHaveCount(0);
      }
    }
  });

  test('ungrouped rows show no #group: tag at all (no chip, since this row has neither description nor group), and named groups still sort before the ungrouped bucket', async ({ page }) => {
    // domcontentloaded, not networkidle — this page's WS reconnect loop
    // keeps the network non-idle indefinitely (same caveat already
    // documented in automation_agent_row_card_styling.spec.js).
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.kind-card-grid')).toBeVisible({ timeout: TIMEOUT });

    await page.locator('.kind-card[data-kind="agents"]').click();
    const groupedRow = page.locator('[role="listitem"]').filter({ hasText: 'Loss Funds Negative E2E' });
    const ungroupedRow = page.locator('[role="listitem"]').filter({ hasText: 'AAA Ungrouped E2E' });
    await expect(groupedRow.locator('.aw-row-desc')).toContainText('#group:hedge-book');
    // CYCLE_ROW_UNGROUPED has neither description nor group — per the
    // 2026-10-12 "reduce space wastage" revision, the whole row is
    // omitted entirely (no "#group:Ungrouped" noise on every plain row).
    await expect(ungroupedRow.locator('.aw-row-desc')).toHaveCount(0);

    // Sort-by-group check: "AAA Ungrouped E2E" would sort FIRST under a
    // plain alphabetical-by-name order — asserting it renders AFTER the
    // "hedge-book" row proves the list is actually sorted by group, not
    // by name.
    const rows = page.locator('[role="listitem"]');
    const names = await rows.locator('.flex-1.min-w-0.flex.flex-col span.truncate').allTextContents();
    const hedgeIdx = names.findIndex((n) => n.includes('Loss Funds Negative E2E'));
    const ungroupedIdx = names.findIndex((n) => n.includes('AAA Ungrouped E2E'));
    expect(hedgeIdx).toBeGreaterThanOrEqual(0);
    expect(ungroupedIdx).toBeGreaterThan(hedgeIdx);
  });
});

test.describe('Brackets kind-card reads live Agent(kind=bracket) data (Sprint E)', () => {
  test('Edit on the Brackets card PATCHes /api/admin/templates/{legacy_template_id}, never {agent.id}', async ({ page }) => {
    // BRACKET_AGENT_ROW.legacy_template_id = 201 (the real, canonical
    // OrderTemplate.id) while its OWN Agent.id = 202 — deliberately
    // different, so a PATCH sent to .../202 instead of .../201 proves
    // the frontend adapter leaked the wrong id (the exact collision
    // risk the backend council-risk review's fix #2 blocked dispatch
    // on, mirrored here on the read/edit side).
    let patchedUrl = /** @type {string | null} */ (null);
    await page.route('**/api/admin/templates/201', async (route) => {
      if (route.request().method() !== 'PATCH') { await route.continue(); return; }
      patchedUrl = route.request().url();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) });
    });
    await page.route('**/api/admin/templates/202', async (route) => {
      // Must never be hit — if it is, patchedUrl above stays null and
      // the test's final assertion fails.
      await route.continue();
    });

    // domcontentloaded, not networkidle — this page's WS reconnect loop
    // keeps the network non-idle indefinitely (same caveat already
    // documented in automation_agent_row_card_styling.spec.js).
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.kind-card-grid')).toBeVisible({ timeout: TIMEOUT });

    await page.locator('.kind-card[data-kind="brackets"]').click();
    const row = page.locator('[role="listitem"]').filter({ hasText: 'E2E Bracket' });
    await expect(row).toBeVisible({ timeout: TIMEOUT });
    await row.locator('[role="button"]').first().click();
    await row.getByRole('button', { name: 'Edit' }).click();

    const saveBtn = page.locator('.console-agent-cli-btn');
    await expect(saveBtn).toBeVisible({ timeout: TIMEOUT });
    await saveBtn.click();

    await expect.poll(() => patchedUrl, { timeout: TIMEOUT }).not.toBeNull();
    expect(/** @type {string} */ (patchedUrl)).toContain('/api/admin/templates/201');
  });
});
