/**
 * console_kind_sample_text.spec.js
 *
 * 2026-10-18 fix (second pass) — operator: "switching between
 * agents/order/bracket/alert does not change cli text sample text. two
 * issues. it stays static. if it changes, it shows as real text
 * entered, instead of erasing it waiting for user inputs."
 *
 * The first pass (now superseded) wrote each kind's sample straight into
 * the real bound `value`, tracked via a hand-rolled `_isSeedActive` flag
 * that compared the live text against the active kind's seed string to
 * decide "is this still an untouched sample." That's exactly the kind of
 * state machine prone to going stale (a switch silently no-ops) or firing
 * when it shouldn't (a sample reads back as committed real content).
 *
 * Fix: samples are now rendered via the textarea's native `placeholder`
 * prop (automation/+page.svelte's `_consolePlaceholder`, threaded through
 * ConsoleTab → AgentCliEditor) instead of being written into `value` at
 * all. A placeholder is never part of the field's value — switching kinds
 * is a plain prop reassignment (always "changes"), and typing anything
 * makes the browser hide it for free (always "erases waiting for input"),
 * with no JS bookkeeping that can drift.
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

test.describe('Console kind dropdown — placeholder sample always reflects the selected kind, never becomes real value', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1500);
  });

  test('switching kind always updates the placeholder, even repeatedly', async ({ page }) => {
    const textarea = page.locator('.aw-view-body textarea, textarea').first();
    await expect(textarea).toHaveValue('', { timeout: TIMEOUT });

    await pickKind(page, 'Bracket');
    await expect(textarea).toHaveAttribute('placeholder', /^bracket\(/, { timeout: TIMEOUT });
    await pickKind(page, 'Agent');
    await expect(textarea).toHaveAttribute('placeholder', /^WHEN /, { timeout: TIMEOUT });
    await pickKind(page, 'Order');
    await expect(textarea).toHaveAttribute('placeholder', /^order\(/, { timeout: TIMEOUT });
    await pickKind(page, 'Alert');
    await expect(textarea).toHaveAttribute('placeholder', /^ALERT /, { timeout: TIMEOUT });

    // The real value is untouched by any of this — only the placeholder changed.
    await expect(textarea).toHaveValue('', { timeout: TIMEOUT });
  });

  test('a placeholder is never real content — focusing/clicking never leaves committed text behind', async ({ page }) => {
    const textarea = page.locator('.aw-view-body textarea, textarea').first();
    await pickKind(page, 'Alert');
    await expect(textarea).toHaveAttribute('placeholder', /^ALERT /, { timeout: TIMEOUT });
    await textarea.click();
    // Native placeholder — clicking/focusing never copies it into the value.
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
