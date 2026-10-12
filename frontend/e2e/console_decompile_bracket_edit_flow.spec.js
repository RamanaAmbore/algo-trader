/**
 * console_decompile_bracket_edit_flow.spec.js — Sprint 18; rewired Sprint B
 * (2026-10, Console retirement); rewired AGAIN for Sprint C (2026-10, Phase 2
 * "Automation redesign").
 *
 * BracketsTab.svelte (and its "Open in Console" create-only button) is
 * DELETED in Sprint C — Brackets are now a kind-card on the Agents tab
 * (`/automation` → "Brackets" card → group tab → row list). Edit on a row
 * decompiles its live JSON straight into the Console tab's CLI textarea
 * AND arms edit mode (`_consoleEditTarget`), so Save now genuinely routes
 * through `PATCH /api/admin/templates/{id}` (update), never
 * `POST /api/admin/templates/` (create) — a real behavior change from the
 * old "Open in Console" button, which this spec's name predates but now
 * accurately describes.
 *
 * Both scenarios intercept `GET /api/admin/templates/` with a fixed
 * fixture row (via `page.route`) instead of creating a real row through
 * the API — `POST/PATCH /api/admin/templates/` requires the
 * `manage_settings` capability (designated role only; the default `rambo`
 * admin fixture lacks it — backend/api/rbac.py). The PATCH call itself is
 * also intercepted so this spec never needs that capability to prove the
 * UPDATE route was actually called with the right id/body. The
 * grammar-tokens catalog fetch (`GET /api/admin/grammar/tokens`) is real
 * network — `view_agents_catalog` already covers the default admin role.
 *
 * Covers:
 *   1. A realistic custom Bracket (slug=null, is_system=false,
 *      is_default=false, is_active=true) decompiles successfully —
 *      clicking Edit switches to the Console tab with the CLI textarea
 *      pre-filled with the correct `bracket(...)` text, an "Editing
 *      bracket ..." banner, and a "Save" button (not "Save bracket")
 *      that PATCHes the SAME row id, never POSTs a new one.
 *   2. A system/default Bracket (is_system=true, is_default=true,
 *      slug set) refuses loud instead — a short toast appears, and the
 *      page stays on the row list (no navigation to Console).
 *
 * Viewport: chromium-desktop only (CLI authoring is not a mobile-first
 * surface — same convention as the sibling console/automation CLI specs).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;

// Sprint E (Agent/Bracket/Template unification phase) — the Brackets
// card's LIST read now comes from `GET /api/agents/` (every
// `kind='bracket'` row, reshaped client-side via
// `bracketAgentToTemplateOut`), never `GET /api/admin/templates/`
// directly. `legacy_template_id` is the real, canonical
// `OrderTemplate.id` (what Edit's PATCH call must target); `id` below
// is this Agent row's own, unrelated primary key — deliberately
// different from `legacy_template_id` in both fixtures so a PATCH
// aimed at the wrong id would fail these tests loudly.
/** @type {any} */
const CUSTOM_BRACKET_AGENT_ROW = {
  id: 95001, slug: 'bracket-90001', name: 'ZZZ E2E Custom Bracket', long_name: null,
  description: '', conditions: {}, events: [], actions: [], scope: 'total',
  schedule: 'market_hours', cooldown_minutes: 30, fire_at_time: null, status: 'active',
  last_triggered_at: null, trigger_count: 0, last_error: null, is_system: false,
  lifespan_type: 'persistent', lifespan_max_fires: null, lifespan_expires_at: null,
  debounce_minutes: 0, tags: [], blackout_windows: [], trade_mode: 'paper', kind: 'bracket',
  tier: 'medium', topic: 'general', group: null, cli_source: null,
  legacy_template_id: 90001,
  bracket_config: {
    applies_to: 'both', tp_pct: 30, sl_pct: -20, wing_premium_pct: null,
    wing_strike_offset: null, wing_max_spread_pct: null, tp_order_type: 'LIMIT',
    tp_scales_json: null, sl_trail_pct: null, is_default: false,
  },
};

/** @type {any} */
const SYSTEM_BRACKET_AGENT_ROW = {
  id: 95002, slug: 'default-bull', name: 'Default Bull', long_name: null,
  description: 'Seeded system default.', conditions: {}, events: [], actions: [],
  scope: 'total', schedule: 'market_hours', cooldown_minutes: 30, fire_at_time: null,
  status: 'active', last_triggered_at: null, trigger_count: 0, last_error: null,
  is_system: true, lifespan_type: 'persistent', lifespan_max_fires: null,
  lifespan_expires_at: null, debounce_minutes: 0, tags: [], blackout_windows: [],
  trade_mode: 'paper', kind: 'bracket', tier: 'medium', topic: 'general', group: null,
  cli_source: null,
  legacy_template_id: 1,
  bracket_config: {
    applies_to: 'buy_any', tp_pct: 30, sl_pct: -20, wing_premium_pct: null,
    wing_strike_offset: null, wing_max_spread_pct: null, tp_order_type: 'LIMIT',
    tp_scales_json: null, sl_trail_pct: null, is_default: true,
  },
};

/** Intercepts GET /api/agents/ with a fixed row list — never hits the
 *  real DB, so these scenarios run identically regardless of what real
 *  Agents/Brackets exist on dev. */
async function mockAgentsList(/** @type {import('@playwright/test').Page} */ page, /** @type {any[]} */ rows) {
  await page.route('**/api/agents/', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(rows) });
  });
}

/** Drill into the Brackets kind-card's (single, ungrouped) row list and
 *  expand the row matching `name`. Returns the row locator, already
 *  expanded. */
async function openBracketRow(/** @type {import('@playwright/test').Page} */ page, /** @type {string} */ name) {
  await page.locator('.kind-card[data-kind="brackets"]').click();
  const row = page.locator('[role="listitem"]').filter({ hasText: name });
  await expect(row).toBeVisible({ timeout: TIMEOUT });
  await row.locator('[role="button"]').first().click();
  return row;
}

test.describe('Brackets kind-card row → Edit → decompileBracket → Console (Sprint C)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('custom Bracket decompiles, pre-fills the Console tab in EDIT mode, and Save PATCHes the same row', async ({ page }) => {
    let createCalled = false;
    // The LIST read is now GET /api/agents/ (Sprint E) — the row's own
    // Agent.id (95001) is deliberately different from
    // legacy_template_id (90001) so a PATCH aimed at the wrong id
    // fails this test loudly.
    await mockAgentsList(page, [CUSTOM_BRACKET_AGENT_ROW]);
    // CRUD (create/patch) is UNCHANGED — still /api/admin/templates/.
    await page.route('**/api/admin/templates/', async (route) => {
      if (route.request().method() === 'POST') { createCalled = true; await route.continue(); return; }
      await route.continue();
    });
    let patchCalled = /** @type {any} */ (null);
    await page.route('**/api/admin/templates/90001', async (route) => {
      if (route.request().method() !== 'PATCH') { await route.continue(); return; }
      patchCalled = { url: route.request().url(), body: route.request().postDataJSON() };
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) });
    });

    // domcontentloaded, not networkidle — /automation's WS reconnect loop
    // keeps the network non-idle indefinitely.
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.kind-card-grid')).toBeVisible({ timeout: TIMEOUT });

    const row = await openBracketRow(page, 'ZZZ E2E Custom Bracket');
    await row.getByRole('button', { name: 'Edit' }).click();

    // Switched to the Console tab.
    await expect(page.locator('.console-edit-banner')).toBeVisible({ timeout: TIMEOUT });
    await expect(page.locator('.console-edit-banner')).toContainText('ZZZ E2E Custom Bracket');

    const cliTextarea = page.locator('.agent-cli-textarea');
    await expect(cliTextarea).toBeVisible({ timeout: TIMEOUT });
    const value = await cliTextarea.inputValue();
    expect(value).toMatch(/^bracket\(/);
    expect(value).toContain('applies_to=both');
    expect(value).toContain('tp_pct=30');
    expect(value).toContain('sl_pct=-20');
    expect(value).toContain('#name="ZZZ E2E Custom Bracket"');

    // Edit mode: the button reads "Save" (update), never "Save bracket"
    // (create) or "Create agent"/"Create token".
    const saveBtn = page.locator('.console-agent-cli-btn');
    await expect(saveBtn).toBeVisible({ timeout: TIMEOUT });
    await expect(saveBtn).toHaveText('Save');

    await saveBtn.click();
    await expect.poll(() => patchCalled, { timeout: TIMEOUT }).not.toBeNull();
    expect(patchCalled.url).toContain('/api/admin/templates/90001');
    expect(createCalled).toBe(false);

    // Edit-mode banner clears on a successful Save.
    await expect(page.locator('.console-edit-banner')).toHaveCount(0);
  });

  test('system Bracket refuses loud instead of switching to Console', async ({ page }) => {
    await mockAgentsList(page, [SYSTEM_BRACKET_AGENT_ROW]);
    // domcontentloaded, not networkidle — /automation's WS reconnect loop
    // keeps the network non-idle indefinitely.
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.kind-card-grid')).toBeVisible({ timeout: TIMEOUT });

    const row = await openBracketRow(page, 'Default Bull');
    // is_system rows have no Delete action, but Edit is still offered —
    // the refusal happens inside editRow()'s own decompile call.
    await row.getByRole('button', { name: 'Edit' }).click();

    await expect(page.locator('.rbq-toast-msg').first()).toBeVisible({ timeout: TIMEOUT });

    // Never switched to the Console tab.
    await expect(page.locator('.console-edit-banner')).toHaveCount(0);
    await expect(page.locator('.agent-cli-textarea')).toHaveCount(0);
  });
});
