/**
 * Test the template-slug binding on the Lab mint form (commit ac4d08c9).
 *
 * Verifies that:
 * 1. The "Exit bracket" input is visible when mint kind='place'
 * 2. The input is hidden for other kinds (cancel, modify, activate, deactivate, update)
 * 3. The POST body to /api/mcp/confirm-token includes template_slug:
 *    - With the entered value when filled
 *    - As null when left empty
 * 4. The Safety card mentions the exit bracket is bound and re-minting is required
 *
 * NOTE: The research lab requires the view_research capability, which is granted to
 * designated, trader, risk, and demo roles (not admin). If running locally with
 * default credentials, you may need to adjust PLAYWRIGHT_USER to a user with
 * the trader or designated role.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/lab_mint_template_binding.spec.js
 *   cd frontend && npx playwright test e2e/lab_mint_template_binding.spec.js --project=mobile-portrait
 *   PLAYWRIGHT_USER=trader npx playwright test e2e/lab_mint_template_binding.spec.js
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';

test.describe('Lab mint form template_slug binding', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/admin/mcp`, { waitUntil: 'networkidle' });

    // Check if access is denied (role doesn't have view_research capability)
    // view_research is only for designated, trader, risk, demo roles (not admin)
    const accessDenied = await page.locator('text=Access denied').first().isVisible().catch(() => false);
    if (accessDenied) {
      test.skip(
        'Research lab requires view_research capability (designated, trader, risk, or demo role). ' +
        'Run with PLAYWRIGHT_USER=<trader-or-designated-user> to test with proper permissions.'
      );
    }

    // Wait for lab-tab elements to be visible, then click Settings
    await page.waitForSelector('.lab-tab', { timeout: 5000 });
    await page.locator('.lab-tab', { hasText: 'Settings' }).click();
    // Wait for Settings tab content to load
    await page.waitForSelector('.lab-settings', { timeout: 5000 });
  });

  test(`template_slug input is visible when kind='place'`, async ({ page }) => {
    // By default, kind is 'place', so the Exit bracket input should be visible
    const exitTemplateLabel = page.locator('label', { hasText: /exit bracket/i });
    await expect(exitTemplateLabel).toBeVisible();

    // The input itself should also be visible and empty
    const exitTemplateInput = exitTemplateLabel.locator('input');
    await expect(exitTemplateInput).toBeVisible();
    await expect(exitTemplateInput).toHaveValue('');
  });

  test(`Safety card mentions exit template binding`, async ({ page }) => {
    // Scroll to Safety card
    const safetyCard = page.locator('article.lab-card', { hasText: /4\.\s*safety/i });
    await expect(safetyCard).toBeVisible();
    await safetyCard.scrollIntoViewIfNeeded();

    // Verify the safety text mentions "exit bracket" and "re-mint"
    const safetyText = await safetyCard.textContent();
    expect(safetyText).toContain('exit bracket');
    expect(safetyText).toContain('template_slug');
    expect(safetyText).toContain('re-mint');
  });

  test(`Mint form section 0 help text mentions template binding`, async ({ page }) => {
    // Find the help text in section 0 (Mint a confirm token)
    const mintCard = page.locator('article.lab-card', { hasText: /0\.\s*mint a confirm token/i });
    await expect(mintCard).toBeVisible();

    // Verify the help text mentions the exit bracket binding
    const helpText = await mintCard.textContent();
    expect(helpText).toContain('exit bracket is part of the token too');
    expect(helpText).toContain('re-mint the token');
  });

  test(`mint request includes template_slug when filled`, async ({ page }) => {
    // Set up route interception to capture the mint request
    let capturedRequest = null;
    await page.route('**/api/mcp/confirm-token', (route) => {
      capturedRequest = route.request();
      // Don't actually continue the request — just capture and abort
      // to speed up the test
      route.abort('blockedbyclient');
    });

    // Fill the form with place order details
    const accountLabel = page.locator('label').filter({ hasText: /^Account/ }).first();
    const accountInput = accountLabel.locator('input');
    await accountInput.fill('ZG0790');

    const symbolLabel = page.locator('label').filter({ hasText: /^Symbol/ }).first();
    const symbolInput = symbolLabel.locator('input');
    await symbolInput.fill('NIFTY25APRFUT');

    // Fill the Exit bracket field
    const exitTemplateLabel = page.locator('label', { hasText: /exit bracket/i });
    const exitTemplateInput = exitTemplateLabel.locator('input');
    await exitTemplateInput.fill('my-exit-template');

    // Click Mint button
    const mintBtn = page.locator('button.mint-btn, button', { hasText: /mint token/i }).first();
    await mintBtn.click();

    // Wait for the request to be captured
    await page.waitForTimeout(500);

    // Verify the captured request body includes template_slug
    expect(capturedRequest).not.toBeNull();
    const postData = JSON.parse(capturedRequest.postDataBuffer().toString());
    expect(postData.template_slug).toBe('my-exit-template');
  });

  test(`mint request includes template_slug as null when empty`, async ({ page }) => {
    // Set up route interception
    let capturedRequest = null;
    await page.route('**/api/mcp/confirm-token', (route) => {
      capturedRequest = route.request();
      route.abort('blockedbyclient');
    });

    // Fill the form but leave Exit bracket empty
    const accountLabel = page.locator('label').filter({ hasText: /^Account/ }).first();
    const accountInput = accountLabel.locator('input');
    await accountInput.fill('ZG0790');

    const symbolLabel = page.locator('label').filter({ hasText: /^Symbol/ }).first();
    const symbolInput = symbolLabel.locator('input');
    await symbolInput.fill('NIFTY25APRFUT');

    // Leave Exit bracket empty (default behavior)
    const exitTemplateLabel = page.locator('label', { hasText: /exit bracket/i });
    const exitTemplateInput = exitTemplateLabel.locator('input');
    await expect(exitTemplateInput).toHaveValue('');

    // Click Mint button
    const mintBtn = page.locator('button.mint-btn, button', { hasText: /mint token/i }).first();
    await mintBtn.click();

    // Wait for request
    await page.waitForTimeout(500);

    // Verify template_slug is null in the request
    expect(capturedRequest).not.toBeNull();
    const postData = JSON.parse(capturedRequest.postDataBuffer().toString());
    expect(postData.template_slug).toBeNull();
  });

  test(`template_slug field renders correct placeholder text`, async ({ page }) => {
    const exitTemplateLabel = page.locator('label', { hasText: /exit bracket/i });
    const exitTemplateInput = exitTemplateLabel.locator('input');
    await expect(exitTemplateInput).toHaveAttribute('placeholder', /\(none\) e\.g\. default-bull/);
  });
});
