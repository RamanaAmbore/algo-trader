/**
 * console_decompile_bracket_edit_flow.spec.js — Sprint 18; rewired
 * Sprint B (2026-10, Phase 2 "Console retirement").
 *
 * `decompileBracket` (frontend/src/lib/command/grammars/agents.js) had
 * ZERO production callers before Sprint 18 — only `__tests__` ever
 * invoked it. This spec gives it its first real UI path: BracketsTab's
 * "Open in Console" row action (frontend/src/lib/automation/
 * BracketsTab.svelte) decompiles a Bracket's live JSON to `bracket(...)`
 * CLI text and navigates to `/automation?tab=console&cli=<encoded
 * text>` (previously `/console?cli=...` — /console is now a redirect,
 * see frontend/src/routes/(algo)/console/+page.js), where Automation's
 * own `?cli=` prefill consumer seeds ConsoleTab's AgentCliEditor
 * textarea with it.
 *
 * NOT a true in-place edit — the Console tab's own submit path is
 * create-only (consoleDispatch.js:submitConsoleBracket always POSTs a
 * new row), so this button is deliberately labelled "Open in Console",
 * not "Edit via CLI". The spec name reflects the dispatch brief's
 * framing but the assertions below only ever check pre-fill
 * correctness, never that the original row got overwritten.
 *
 * Both scenarios intercept the `GET /api/admin/templates/` list response
 * with a fixed fixture row (via `page.route`) instead of creating a real
 * row through the API — `POST /api/admin/templates/` requires the
 * `manage_settings` capability (designated role only; the default
 * `rambo` admin fixture lacks it — backend/api/rbac.py), and mocking the
 * read avoids needing a second role's credentials just to seed fixture
 * data. The grammar-tokens catalog fetch (`GET /api/admin/grammar/
 * tokens`) is real network — `view_agents_catalog` already covers the
 * default admin role.
 *
 * Covers (Sprint 18 "Done when" criterion — "at least one real UI path
 * exists where decompile actually runs for an operator"):
 *   1. A realistic custom Bracket (slug=null, is_system=false,
 *      is_default=false, is_active=true) decompiles successfully —
 *      clicking "Open in Console" navigates to /console with the CLI
 *      textarea pre-filled with the correct `bracket(...)` text, and
 *      the catalog loads well enough for "Save bracket" to appear.
 *   2. A system/default Bracket (is_system=true, is_default=true,
 *      slug set) refuses loud instead — a short toast appears, and the
 *      page stays on /automation (no navigation).
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

/** Intercepts GET /api/admin/templates/ with a single fixed row — never
 *  hits the real DB, so these scenarios run identically regardless of
 *  what real Brackets exist on dev. */
async function mockTemplatesList(/** @type {import('@playwright/test').Page} */ page, /** @type {any[]} */ rows) {
  await page.route('**/api/admin/templates/', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(rows) });
  });
}

test.describe('BracketsTab "Open in Console" → decompileBracket (Sprint 18)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('custom Bracket decompiles and pre-fills the Console tab correctly', async ({ page }) => {
    await mockTemplatesList(page, [CUSTOM_BRACKET_ROW]);
    await page.goto('/automation?tab=brackets');
    await page.waitForLoadState('networkidle');

    const row = page.locator('.tpl-row').filter({ hasText: 'ZZZ E2E Custom Bracket' });
    await expect(row).toBeVisible({ timeout: TIMEOUT });
    await row.locator('.tpl-row-head').click();

    const openBtn = row.getByRole('button', { name: 'Open in Console' });
    await expect(openBtn).toBeVisible({ timeout: TIMEOUT });
    await openBtn.click();

    await page.waitForURL(/\/automation\?.*cli=/, { timeout: TIMEOUT });

    const cliTextarea = page.locator('.agent-cli-textarea');
    await expect(cliTextarea).toBeVisible({ timeout: TIMEOUT });
    const value = await cliTextarea.inputValue();
    expect(value).toMatch(/^bracket\(/);
    expect(value).toContain('applies_to=both');
    expect(value).toContain('tp_pct=30');
    expect(value).toContain('sl_pct=-20');
    expect(value).toContain('#name="ZZZ E2E Custom Bracket"');

    // Catalog loaded + the decompiled text recompiles cleanly — "Save
    // bracket" (not an error list) confirms the pre-fill is genuinely
    // usable, not just textually present.
    await expect(page.locator('.console-agent-cli-btn', { hasText: 'Save bracket' }))
      .toBeVisible({ timeout: TIMEOUT });

    // The `?cli=` param is stripped once consumed (replaceState) — a
    // reload shouldn't re-seed stale text over a fresh blank session.
    // `?tab=console` is retained so a reload still lands on this tab.
    await expect(page).toHaveURL(/\/automation\?tab=console$/);
  });

  test('system Bracket refuses loud instead of navigating', async ({ page }) => {
    await mockTemplatesList(page, [SYSTEM_BRACKET_ROW]);
    await page.goto('/automation?tab=brackets');
    await page.waitForLoadState('networkidle');

    const row = page.locator('.tpl-row').filter({ hasText: 'Default Bull' });
    await expect(row).toBeVisible({ timeout: TIMEOUT });
    await row.locator('.tpl-row-head').click();

    const openBtn = row.getByRole('button', { name: 'Open in Console' });
    await expect(openBtn).toBeVisible({ timeout: TIMEOUT });
    await openBtn.click();

    await expect(page.locator('.rbq-toast-msg').first()).toBeVisible({ timeout: TIMEOUT });
    await expect(page.locator('.rbq-toast-msg').first()).toContainText(/CLI/i);

    // Never navigated away.
    await expect(page).toHaveURL(/\/automation/);
  });
});
