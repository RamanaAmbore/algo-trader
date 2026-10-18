/**
 * log_panel_news_order_no_system_leak.spec.js
 *
 * 2026-10-18 fix — operator: "in dashboard, I am seeing the area with
 * the text no system area yet. It is newly showing... it is showing for
 * news tab. it should not be there, added recently."
 *
 * Root cause: LogPanel.svelte's row-renderer block (terminal / agent /
 * alerts / simulator / conn / system) sat in its own `<div>`,
 * UNCONDITIONALLY rendered immediately after the outer
 * `{#if logTab === 'news'} ... {:else if logTab === 'order'} ... {:else}
 * {/if}` chain closed — it had no gate excluding 'news'/'order', which
 * are already FULLY rendered by that outer chain (NewsList / OrderCard).
 * Since the row-renderer's own inner chain has no branch matching
 * 'news' or 'order' either, both fell through to its bare `{:else}`
 * (the "system" empty-state), appending "No system entries yet."
 * underneath the real News/Order content on every page that embeds
 * LogPanel with `defaultTab="news"` (the Dashboard activity card) or
 * whenever the operator switched to the Order tab anywhere LogPanel is
 * mounted.
 *
 * Fix: gate that whole block on `logTab !== 'news' && logTab !== 'order'`.
 *
 * Run:
 *   npx playwright test e2e/log_panel_news_order_no_system_leak.spec.js
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;

test.describe('LogPanel — News/Order tabs never leak the "No system entries yet." empty state', () => {
  test('Dashboard activity card (defaultTab="news") does not show the system empty-state', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2000);
    expect(await page.getByText('No system entries yet.').count()).toBe(0);
  });

  test('switching to the Order tab does not show the system empty-state underneath it', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1500);
    const orderTabBtn = page.locator('button, [role="tab"]').filter({ hasText: /^Order/i }).first();
    const visible = await orderTabBtn.isVisible({ timeout: TIMEOUT }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: 'Order tab not visible in this viewport/role' });
      return;
    }
    await orderTabBtn.click();
    await page.waitForTimeout(500);
    expect(await page.getByText('No system entries yet.').count()).toBe(0);
  });

  test('the System tab itself still renders its own empty-state correctly when there are no entries', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1500);
    const sysTabBtn = page.locator('button, [role="tab"]').filter({ hasText: /^System/i }).first();
    const visible = await sysTabBtn.isVisible({ timeout: TIMEOUT }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: 'System tab not visible (view_audit cap gate or layout)' });
      return;
    }
    await sysTabBtn.click();
    await page.waitForTimeout(500);
    // The fix must not have also broken the System tab's OWN legitimate
    // empty-state — it should still be reachable when logTab is actually 'system'.
    const sysMsg = page.getByText('No system entries yet.');
    const hasRows = await page.locator('.log-row').count();
    if (hasRows === 0) {
      await expect(sysMsg).toBeVisible({ timeout: TIMEOUT });
    }
  });
});
