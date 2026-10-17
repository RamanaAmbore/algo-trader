/**
 * Order entry — three-tab shell smoke.
 *
 * STALE WARNING (pre-existing, not caused by this task): `/console` was
 * retired as a standalone route in Sprint B (2026-10, "Console
 * retirement") and now 308-redirects to `/automation?tab=console` — it
 * no longer renders the inline three-tab OrderEntryShell these tests
 * describe (Command line / Order ticket / Chain). Every test below that
 * `page.goto('/console')`s and expects those tabs was already broken by
 * that redirect before this task touched anything; none of them have
 * been fixed here — flagged per the operator's own task scope ("the
 * file's other `/console` assumptions were already stale before this
 * task; flag them, don't fix them").
 *
 * The two tests that were ONLY about the now-deleted "Command line" tab
 * and CommandBar.svelte chip dropdown (2026-10-17, operator-confirmed
 * removal — see docs/specs/AUTOMATION_SPEC.md's 2026-10-17 change-log
 * row) were removed below, since that surface no longer exists in any
 * form to fix forward to. The remaining tests are left as-is (still
 * broken by the pre-existing redirect, unrelated to this task).
 *
 * Run against prod: PLAYWRIGHT_BASE_URL=https://ramboq.com npx playwright test order_entry_tabs
 */

import { test, expect } from '@playwright/test';

const TIMEOUT = 30_000;

test.describe('Order entry — 3 tabs', () => {

  test('switching tabs swaps active surface', async ({ page }) => {
    await page.goto('/console');
    if (page.url().includes('/signin')) test.skip(true, 'signin gate');

    const ticketTab = page.getByRole('tab', { name: /Order ticket/i });
    await ticketTab.click();
    await expect(ticketTab).toHaveAttribute('aria-selected', 'true');

    // Ticket body is visible — the side toggle (BUY/SELL) is the
    // canonical signal it mounted.
    await expect(page.locator('.ot-side-toggle')).toBeVisible({ timeout: TIMEOUT });

    const chainTab = page.getByRole('tab', { name: /Chain/i });
    await chainTab.click();
    await expect(chainTab).toHaveAttribute('aria-selected', 'true');
  });

  test('basket bar appears when a leg is added (Chain tab)', async ({ page }) => {
    await page.goto('/console');
    if (page.url().includes('/signin')) test.skip(true, 'signin gate');

    await page.getByRole('tab', { name: /Chain/i }).click();
    // Without an underlying picked the chain is empty; the basket-bar
    // doesn't render until a leg is added. We verify the chain UI
    // surfaces (the underlying picker / "+ Add" affordance).
    const chainShell = page.locator('.octa-shell, .oct-root, [data-tab="chain"]').first();
    await expect(chainShell).toBeVisible({ timeout: TIMEOUT });
  });

  test('+ Basket button visible on Ticket tab in shell', async ({ page }) => {
    await page.goto('/console');
    if (page.url().includes('/signin')) test.skip(true, 'signin gate');
    await page.getByRole('tab', { name: /Order ticket/i }).click();
    await expect(page.locator('.ot-basket').first()).toBeVisible({ timeout: 10000 });
  });

  test('navbar mode chip is present and clickable (admin only)', async ({ page }) => {
    await page.goto('/dashboard');
    if (page.url().includes('/signin')) test.skip(true, 'signin gate');

    const modeChip = page.locator('.mode-trigger').first();
    // The chip only renders for authenticated users. Demo (anonymous on
    // prod) doesn't see it — skip rather than fail.
    if (!(await modeChip.count())) {
      test.skip(true, 'demo session — mode chip is admin-only');
    }
    await expect(modeChip).toBeVisible();
    await modeChip.click();
    await expect(page.locator('.mode-combo-dropdown')).toBeVisible({ timeout: 5000 });
  });
});
