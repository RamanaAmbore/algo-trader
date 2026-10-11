/**
 * LogPanel Alerts tab — 2026-10 nav restructure regression spec.
 *
 * The standalone /admin/alerts page was retired and its functionality
 * folded into LogPanel as a new 'alerts' tab (display label "Alert
 * Log" as of Sprint C, 2026-10 — see that tab's own comment in
 * LogPanel.svelte), gated by the view_audit capability. It is mounted
 * ONLY on /automation, as part of the already-existing bottom-attached
 * ActivityLogSurface strip (same structural role Orders' own activity
 * card plays — always visible below whatever top-level AutomationTabs
 * sub-view is active, not a tab inside AutomationTabs itself).
 *
 * Five quality dimensions (per feedback_test_dimensions.md):
 *   SSOT     — the view_audit cap gate lives once, inside LogPanel's own
 *              VISIBLE_TABS filter; no page-level duplicate gate.
 *   Perf     — the strip is a single LogPanel instance shared by every
 *              AutomationTabs sub-view (Console/Agents), not remounted
 *              per tab switch.
 *   Stale    — source-scan confirms the deleted /admin/alerts route is
 *              gone from disk and from the nav link array; no dangling
 *              href elsewhere in the app.
 *   Reusable — Alert Log rows render through the same _logRow()-based
 *              convention every other LogPanel tab uses (time chip /
 *              level chip / message / tag), not a bespoke layout.
 *   UX       — bottom strip stays mounted across AutomationTabs
 *              sub-views (Console/Agents); an operator without
 *              view_audit never sees the tab at all.
 */

import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'node:url';
import { loginAsAdmin } from './fixtures/auth.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);

test.describe('LogPanel Alerts tab on /automation', () => {
  test.setTimeout(60_000);

  test('authorized (view_audit) operator sees the Alerts tab and it renders rows or an empty state', async ({ page }) => {
    await loginAsAdmin(page); // rambo — admin role, holds view_audit per rbac.py CAPS
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });

    // Bottom-attached strip renders regardless of which AutomationTabs
    // sub-view is active — confirm it's present on first paint (default
    // sub-view is Agents).
    const strip = page.locator('.lp-header-wrap').first();
    await expect(strip).toBeVisible({ timeout: 15_000 });

    const alertsTab = page.getByRole('tab', { name: 'Alert Log' });
    await expect(alertsTab, 'Alert Log tab must be visible for a view_audit-capable operator').toBeVisible({ timeout: 10_000 });

    // Strip stays mounted (not remounted/torn down) when switching the
    // page's own top-level AutomationTabs sub-view — the same
    // always-visible structural role Orders' own activity card plays.
    // AutomationTabs is ALSO an AlgoTabs instance (role="tab"), scoped
    // to its own wrapper so "Agents" doesn't collide with LogPanel's
    // identically-labelled tab inside the bottom strip. Sprint C
    // (2026-10, Phase 2 "Automation redesign") removed the Brackets/
    // Tokens top-level sub-views — Console is the only other one left.
    const topTabs = page.locator('.aw-tabs-wrap');
    const consoleTab = topTabs.getByRole('tab', { name: 'Console' });
    if (await consoleTab.count()) {
      await consoleTab.click();
      await expect(strip, 'bottom strip must stay mounted across AutomationTabs sub-views').toBeVisible();
      await expect(page.getByRole('tab', { name: 'Alert Log' })).toBeVisible();
      // Return to Agents so later assertions aren't order-dependent.
      await topTabs.getByRole('tab', { name: 'Agents' }).click();
    }

    await alertsTab.click();
    // Either real rows or the explicit empty state — never a blank panel.
    const rowsOrEmpty = page.locator('.log-row').first();
    await expect(rowsOrEmpty, 'Alert Log tab must render rows or the "No alert events yet." empty state').toBeVisible({ timeout: 10_000 });
  });

  test('unauthorized (no view_audit) operator never sees the Alert Log tab', async ({ page }) => {
    // Strip view_audit from the /whoami response so the SAME admin
    // account renders as if it lacked the capability — avoids needing
    // a separate partner/risk-role fixture account.
    await page.route('**/api/auth/whoami', async (route) => {
      const resp = await route.fetch();
      let json;
      try { json = await resp.json(); } catch { json = null; }
      if (json && Array.isArray(json.caps)) {
        json.caps = json.caps.filter((c) => c !== 'view_audit');
      }
      await route.fulfill({ response: resp, json: json ?? undefined });
    });

    await loginAsAdmin(page);
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });

    const strip = page.locator('.lp-header-wrap').first();
    await expect(strip).toBeVisible({ timeout: 15_000 });

    // Give the tab strip a moment to settle (poll-driven caps bridge),
    // then assert Alert Log never appears.
    await page.waitForTimeout(1500);
    await expect(page.getByRole('tab', { name: 'Alert Log' }), 'Alert Log tab must stay hidden without view_audit').toHaveCount(0);

    // The rest of the strip must still work normally — Agents tab is
    // always visible regardless of the cap gate. Scoped to the bottom
    // log strip specifically — /automation's own top-level AutomationTabs
    // sub-view switcher also has an "Agents" entry (a different
    // AlgoTabs instance, non-compact), so an unscoped role lookup
    // ambiguously matches both.
    await expect(strip.getByRole('tab', { name: 'Agents' })).toBeVisible();
  });

  test('visiting the deleted /admin/alerts route no longer renders the old Alerts page', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/alerts', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1000);

    // The old page's own title chip must be gone — whatever SvelteKit's
    // default not-found behaviour renders instead, it is NOT the
    // retired Alerts page.
    const oldTitle = page.locator('.page-title-chip', { hasText: 'Alerts' });
    await expect(oldTitle, 'the retired /admin/alerts page must not render').toHaveCount(0);
  });
});

test.describe('[STALE] source-scan — /admin/alerts route fully removed', () => {
  test('route directory deleted from disk', () => {
    const routeDir = path.resolve(__dirname, '..', 'src', 'routes', '(algo)', 'admin', 'alerts');
    expect(fs.existsSync(routeDir), `${routeDir} must not exist`).toBe(false);
  });

  test('no nav link entry still points at /admin/alerts', () => {
    const layoutPath = path.resolve(__dirname, '..', 'src', 'routes', '(algo)', '+layout.svelte');
    const src = fs.readFileSync(layoutPath, 'utf8');
    expect(src, '+layout.svelte must not carry a nav entry for /admin/alerts')
      .not.toMatch(/href:\s*['"]\/admin\/alerts['"]/);
  });

  test('LogPanel carries the alerts tab definition, gated by view_audit', () => {
    const logPanelPath = path.resolve(__dirname, '..', 'src', 'lib', 'LogPanel.svelte');
    const src = fs.readFileSync(logPanelPath, 'utf8');
    expect(src, 'LogPanel must define an alerts tab labelled "Alert Log"').toMatch(/\['alerts',\s*'Alert Log'\]/);
    expect(src, 'LogPanel must gate the alerts tab by view_audit').toContain("hasCap('view_audit'");
  });

  test('only /automation opts the alerts tab into its tabs= prop', () => {
    // Every other LogPanel mount relies on the default `tabs` prop
    // (no explicit override), so a repo-wide grep for the literal
    // "'alerts'" inside a `tabs={[...]}` prop value should find exactly
    // one callsite — /automation's own ActivityLogSurface mount.
    const automationPath = path.resolve(__dirname, '..', 'src', 'routes', '(algo)', 'automation', '+page.svelte');
    const automationSrc = fs.readFileSync(automationPath, 'utf8');
    expect(automationSrc, '/automation must pass tabs= including alerts')
      .toMatch(/tabs=\{\[[^\]]*'alerts'[^\]]*\]\}/);

    const srcRoot = path.resolve(__dirname, '..', 'src');
    /** @type {string[]} */
    const offenders = [];
    function walk(dir) {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) { walk(full); continue; }
        if (!entry.name.endsWith('.svelte')) continue;
        if (full === automationPath) continue;
        const text = fs.readFileSync(full, 'utf8');
        if (/tabs=\{\[[^\]]*'alerts'[^\]]*\]\}/.test(text)) offenders.push(full);
      }
    }
    walk(srcRoot);
    expect(offenders, `unexpected alerts-tab opt-in outside /automation: ${offenders.join(', ')}`).toHaveLength(0);
  });
});
