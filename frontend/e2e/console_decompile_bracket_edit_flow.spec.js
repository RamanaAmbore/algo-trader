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

/** @type {any} */
const CUSTOM_BRACKET_ROW = {
  id: 90001,
  slug: null,
  name: 'ZZZ E2E Custom Bracket',
  description: '',
  applies_to: 'both',
  tp_pct: 30,
  sl_pct: -20,
  wing_premium_pct: null,
  wing_strike_offset: null,
  wing_max_spread_pct: null,
  tp_order_type: 'LIMIT',
  tp_scales_json: null,
  sl_trail_pct: null,
  is_default: false,
  is_system: false,
  is_active: true,
  group: null,
  tags: [],
};

/** @type {any} */
const SYSTEM_BRACKET_ROW = {
  id: 1,
  slug: 'default-bull',
  name: 'Default Bull',
  description: 'Seeded system default.',
  applies_to: 'buy_any',
  tp_pct: 30,
  sl_pct: -20,
  wing_premium_pct: null,
  wing_strike_offset: null,
  wing_max_spread_pct: null,
  tp_order_type: 'LIMIT',
  tp_scales_json: null,
  sl_trail_pct: null,
  is_default: true,
  is_system: true,
  is_active: true,
  group: null,
  tags: [],
};

/** Intercepts GET /api/admin/templates/ with a fixed row list — never
 *  hits the real DB, so these scenarios run identically regardless of
 *  what real Brackets exist on dev. */
async function mockTemplatesList(/** @type {import('@playwright/test').Page} */ page, /** @type {any[]} */ rows) {
  await page.route('**/api/admin/templates/', async (route) => {
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
    // ONE handler for the collection endpoint, discriminating by method —
    // registering a second `page.route` for the same glob pattern would
    // shadow this one (Playwright dispatches to the MOST RECENTLY
    // registered matching handler only; an unconditional `route.continue()`
    // in a later handler bypasses this one entirely, including for GET).
    await page.route('**/api/admin/templates/', async (route) => {
      if (route.request().method() === 'POST') { createCalled = true; await route.continue(); return; }
      if (route.request().method() !== 'GET') { await route.continue(); return; }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([CUSTOM_BRACKET_ROW]) });
    });
    let patchCalled = /** @type {any} */ (null);
    await page.route('**/api/admin/templates/90001', async (route) => {
      if (route.request().method() !== 'PATCH') { await route.continue(); return; }
      patchCalled = { url: route.request().url(), body: route.request().postDataJSON() };
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CUSTOM_BRACKET_ROW) });
    });

    await page.goto('/automation');
    await page.waitForLoadState('networkidle');

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
    await mockTemplatesList(page, [SYSTEM_BRACKET_ROW]);
    await page.goto('/automation');
    await page.waitForLoadState('networkidle');

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
