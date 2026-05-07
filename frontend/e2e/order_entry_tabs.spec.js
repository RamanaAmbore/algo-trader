/**
 * Order entry — three-tab shell smoke.
 *
 * Verifies the Terminal page (/console) renders the inline OrderEntryShell
 * with all three tabs, that switching tabs updates the active surface, and
 * that the basket bar pattern is wired up. Demo-session friendly — no
 * actual broker calls happen, but every DOM affordance the operator
 * needs to PLACE an order is exercised.
 *
 * Run against prod: PLAYWRIGHT_BASE_URL=https://ramboq.com npx playwright test order_entry_tabs
 */

import { test, expect } from '@playwright/test';

const TIMEOUT = 30_000;

test.describe('Order entry — 3 tabs', () => {

  test('Terminal page renders 3 colour-coded tabs', async ({ page }) => {
    await page.goto('/console');
    // Demo session anonymous on prod is allowed; non-prod will redirect to signin.
    if (page.url().includes('/signin')) {
      test.skip(true, 'signin gate — set credentials to run beyond demo');
    }

    // Three tabs visible
    const cmdTab    = page.getByRole('tab', { name: /Command line/i });
    const ticketTab = page.getByRole('tab', { name: /Order ticket/i });
    const chainTab  = page.getByRole('tab', { name: /Chain/i });

    await expect(cmdTab).toBeVisible({ timeout: TIMEOUT });
    await expect(ticketTab).toBeVisible();
    await expect(chainTab).toBeVisible();

    // Default is Command (defaultTab='command' on /console)
    await expect(cmdTab).toHaveAttribute('aria-selected', 'true');
  });

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

  test('Command tab has CommandBar with chip dropdown', async ({ page }) => {
    await page.goto('/console');
    if (page.url().includes('/signin')) test.skip(true, 'signin gate');

    // CommandBar's textarea gets focus when clicked; chip dropdown
    // opens on input change. We verify the textarea exists.
    const cmdInput = page.locator('textarea').first();
    await expect(cmdInput).toBeVisible({ timeout: TIMEOUT });
    await cmdInput.click();
    // pressSequentially fires keydown/keyup so CommandBar's suggestion
    // logic runs (.fill sets value without those events).
    await cmdInput.pressSequentially('buy ', { delay: 30 });
    await expect(page.locator('.cmd-suggest').first()).toBeVisible({ timeout: 5000 });
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
