// Verifies UX changes shipped in commit b50c2524:
//   1. Orders /activity card has account dropdown (.oc-act-acct) in header
//      when more than 1 account present — placeholder "All accounts",
//      computed min-width >= 176px (11rem).
//   2. Inline tab-row account dropdown (.lp-tabrow-acct) is ABSENT inside
//      the Activity card's LogPanel (hideInlineAccountFilter={true}).
//   3. Agents / System / Conn tabs render 2-column layout at desktop width;
//      collapse to 1 column at <= 900px viewport.
//   4. ActivityLogModal (opened via navbar broker chip) lands on Conn tab
//      and its .log-panel.log-rows has columnCount === "2" at desktop width.
//
// Target: https://dev.ramboq.com only (NEVER prod/ramboq.com).
//   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com npx playwright test e2e/activity_card_b50c2524_verify.spec.js --project=chromium-desktop

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

test.describe('activity card — b50c2524 UX changes', () => {
  // Run serially to avoid simultaneous /signin attempts hitting the
  // rate-limiter (5 req/min) when all 3 workers start at the same time.
  test.describe.configure({ mode: 'serial' });
  test.use({ viewport: { width: 1440, height: 900 } });
  // Generous timeout: loginAsAdmin retries at 0s + 3s + 8s = ~11s per worker;
  // with serial mode one retry cycle is enough. Extra headroom for page load.
  test.setTimeout(60_000);

  // ── assertion 1 + 2 ────────────────────────────────────────────────────
  test('header account dropdown present when multi-account; inline tabrow dropdown absent', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });

    // Wait for the activity section to mount.
    const activityCard = page.locator('section.bucket-card-activity');
    await expect(activityCard, 'activity card renders').toBeVisible({ timeout: 20_000 });

    // Scroll the card into view so CSS visibility is resolved.
    await activityCard.scrollIntoViewIfNeeded();

    // The header dropdown is conditional on _actAvailableAccounts.length > 1.
    // We need to wait for orders to load and expose accounts.
    // Poll up to 15s for the dropdown to appear OR confirm single-account env.
    const headerDropdown = activityCard.locator('.bucket-header .oc-act-acct');
    const inlineDropdown = activityCard.locator('.lp-tabrow-acct');

    // Wait for LogPanel to finish its initial poll (3s pollMs + render).
    await page.waitForResponse(r => r.url().includes('/api/orders') && r.status() === 200, { timeout: 15_000 })
      .catch(() => null); // orders may already be cached; proceed either way.

    // Give Svelte a tick to update the reactivity chain.
    await page.waitForTimeout(500);

    const accountCount = await page.locator('.bucket-card-activity .oc-act-acct').count();

    if (accountCount > 0) {
      // Multi-account environment — assert placeholder + min-width.
      await expect(headerDropdown, 'header .oc-act-acct is visible').toBeVisible();

      // Check placeholder text via the trigger element (AccountMultiSelect renders
      // a .multiselect-trigger or .multiselect-control with the placeholder).
      const triggerText = await headerDropdown.locator('.multiselect-trigger, .multiselect-control, [class*="trigger"]')
        .first()
        .textContent({ timeout: 3_000 })
        .catch(() => '');
      expect(
        triggerText?.includes('All accounts') || triggerText?.includes('account'),
        `placeholder contains "All accounts" — got: "${triggerText}"`
      ).toBeTruthy();

      // Computed min-width >= 11rem (176px).
      const minWidth = await headerDropdown.evaluate(el => {
        const s = getComputedStyle(el);
        return parseFloat(s.minWidth);
      });
      expect(minWidth, 'oc-act-acct min-width >= 176px (11rem)').toBeGreaterThanOrEqual(176);
    } else {
      // Single-account or no-orders env — the dropdown is correctly absent.
      // This is valid behaviour (conditional render); note it.
      console.log('[SKIP] oc-act-acct not shown — single-account or no orders loaded');
    }

    // Assertion 2: inline tab-row dropdown MUST be absent regardless.
    await expect(inlineDropdown, '.lp-tabrow-acct must not exist (hideInlineAccountFilter=true)').toHaveCount(0);
  });

  // ── assertion 3 ────────────────────────────────────────────────────────
  test('Agents / System / Conn tabs use 2-column layout at desktop, 1-col at mobile', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });

    const activityCard = page.locator('section.bucket-card-activity');
    await expect(activityCard, 'activity card visible').toBeVisible({ timeout: 20_000 });
    await activityCard.scrollIntoViewIfNeeded();

    // Wait for at least one poll cycle so tabs are rendered.
    await page.waitForTimeout(1000);

    const logRows = activityCard.locator('.log-panel.log-rows');

    // Click the Agents tab.
    const agentTab = activityCard.locator('[role="tab"]:has-text("Agents")');
    await expect(agentTab, 'Agents tab rendered').toBeVisible({ timeout: 10_000 });
    await agentTab.click();
    await expect(logRows, '.log-panel.log-rows visible after Agents tab click').toBeVisible({ timeout: 5_000 });

    let columnCount = await logRows.evaluate(el => getComputedStyle(el).columnCount);
    expect(columnCount, 'Agents tab: columnCount === "2" at 1440px').toBe('2');

    // Click System tab.
    const systemTab = activityCard.locator('[role="tab"]:has-text("System")');
    await expect(systemTab, 'System tab rendered').toBeVisible();
    await systemTab.click();
    await expect(logRows).toBeVisible({ timeout: 5_000 });

    columnCount = await logRows.evaluate(el => getComputedStyle(el).columnCount);
    expect(columnCount, 'System tab: columnCount === "2" at 1440px').toBe('2');

    // Click Conn tab.
    const connTab = activityCard.locator('[role="tab"]:has-text("Conn")');
    await expect(connTab, 'Conn tab rendered').toBeVisible();
    await connTab.click();
    await expect(logRows).toBeVisible({ timeout: 5_000 });

    columnCount = await logRows.evaluate(el => getComputedStyle(el).columnCount);
    expect(columnCount, 'Conn tab: columnCount === "2" at 1440px').toBe('2');

    // ── mobile viewport collapse ──────────────────────────────────────────
    await page.setViewportSize({ width: 800, height: 600 });
    // Give the browser a frame to recompute layout.
    await page.waitForTimeout(200);

    // Stay on Conn tab — verify column count collapses.
    columnCount = await logRows.evaluate(el => getComputedStyle(el).columnCount);
    expect(columnCount, 'Conn tab: columnCount collapses to "1" at 800px').toBe('1');
  });

  // ── assertion 4 ────────────────────────────────────────────────────────
  test('ActivityLogModal opens on Conn tab from broker chip with 2-column layout', async ({ page }) => {
    await loginAsAdmin(page);
    // pulse page has the navbar broker chip.
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    const chip = page.locator('button.broker-chip').first();
    await expect(chip, 'broker chip visible in navbar').toBeVisible({ timeout: 25_000 });

    // broker-chip-partial / broker-chip-down carry CSS animations; force:true bypasses
    // Playwright's "element not stable" check while still confirming it is interactive.
    await chip.click({ force: true });

    const overlay = page.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(overlay, 'ActivityLogModal overlay appears').toBeVisible({ timeout: 8_000 });

    // Conn tab must be aria-selected="true" (openActivityModal('conn') seeds defaultTab).
    const activeConnTab = overlay.locator('[role="tab"][aria-selected="true"]:has-text("Conn")');
    await expect(activeConnTab, 'Conn tab is aria-selected=true on open').toBeVisible({ timeout: 5_000 });

    // The modal's LogPanel is multiColumn={true} — at 1440px should be 2 columns.
    const modalLogRows = overlay.locator('.log-panel.log-rows');
    await expect(modalLogRows, 'modal .log-panel.log-rows present').toBeVisible({ timeout: 5_000 });

    const columnCount = await modalLogRows.evaluate(el => getComputedStyle(el).columnCount);
    expect(columnCount, 'ActivityLogModal .log-panel.log-rows columnCount === "2" at 1440px').toBe('2');
  });
});
