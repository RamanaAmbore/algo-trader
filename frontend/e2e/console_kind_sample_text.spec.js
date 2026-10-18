/**
 * console_kind_sample_text.spec.js
 *
 * 2026-10-18 fix — operator: "it should sample agent when agent is
 * selected. when order is selected, it should show sample order,
 * similarly alerts and brackets. but when keep cursor, it should be
 * cleared. Presently, the sample is not changing. when you click
 * inside, it still keeps the sample text. for agent, it works
 * correctly."
 *
 * Original design only seeded the CLI textarea when it was EMPTY —
 * switching the kind dropdown from one sample to another silently did
 * nothing once any sample was already showing, and focusing the
 * textarea never cleared a sample. Revised design, tracked by the new
 * `_isSeedActive` flag (automation/+page.svelte):
 *   - picking a kind ALWAYS re-samples while the box still shows an
 *     untouched sample (not real typed/edited text)
 *   - focusing the textarea clears an untouched sample to empty
 *   - real typed text, or a real agent loaded via the Agents-tab Edit
 *     action, is NEVER auto-cleared or auto-replaced by either path
 *
 * Run:
 *   npx playwright test e2e/console_kind_sample_text.spec.js
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;

async function pickKind(page, label) {
  await page.locator('.aw-kind-select .rbq-select-trigger').click();
  await page.waitForTimeout(150);
  await page.getByText(label, { exact: true }).first().click();
  await page.waitForTimeout(150);
}

test.describe('Console kind dropdown — sample text re-seeds, clears on focus, never clobbers real text', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1500);
  });

  test('switching kind always re-samples, even when a different sample is already showing', async ({ page }) => {
    const textarea = page.locator('.aw-view-body textarea, textarea').first();
    await pickKind(page, 'Bracket');
    await expect(textarea).toHaveValue(/^bracket\(/, { timeout: TIMEOUT });
    await pickKind(page, 'Agent');
    await expect(textarea).toHaveValue(/^WHEN /, { timeout: TIMEOUT });
    await pickKind(page, 'Order');
    await expect(textarea).toHaveValue(/^order\(/, { timeout: TIMEOUT });
  });

  test('focusing the textarea clears an untouched sample to empty', async ({ page }) => {
    const textarea = page.locator('.aw-view-body textarea, textarea').first();
    await pickKind(page, 'Alert');
    await expect(textarea).toHaveValue(/^ALERT /, { timeout: TIMEOUT });
    await textarea.click();
    await expect(textarea).toHaveValue('', { timeout: TIMEOUT });
  });

  test('real typed text is never overwritten by switching kinds', async ({ page }) => {
    const textarea = page.locator('.aw-view-body textarea, textarea').first();
    const realText = 'order(account="ZG0790", symbol="NIFTY25JULFUT", side="BUY", lots=2)';
    await textarea.fill(realText);
    await pickKind(page, 'Alert');
    await expect(textarea).toHaveValue(realText, { timeout: TIMEOUT });
  });

  test('real typed text is never cleared by focusing the textarea again', async ({ page }) => {
    const textarea = page.locator('.aw-view-body textarea, textarea').first();
    const realText = 'ALERT telegram, email';
    await textarea.fill(realText);
    await textarea.blur();
    await textarea.click();
    await expect(textarea).toHaveValue(realText, { timeout: TIMEOUT });
  });
});
