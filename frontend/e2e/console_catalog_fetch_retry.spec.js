/**
 * console_catalog_fetch_retry.spec.js
 *
 * Live operator report (2026-11-05): "CLI catalog unavailable" sometimes
 * appears while editing an agent in the Console tab, and the help-info
 * panel for reserved words (when/do/etc.) doesn't show — root cause was
 * NOT the help-info lookup itself (that had its own separate gap, fixed
 * the same day — see describeTokenAt.test.js), but that ConsoleTab.svelte
 * replaced the ENTIRE AgentCliEditor textbox with a dead-end error
 * message on a transient `/api/admin/grammar/tokens` fetch failure (auth-
 * token-not-yet-attached race, a momentary network blip — same bug class
 * as the templates.js "module-level fetch cache stuck on failure"
 * incident CLAUDE.md documents), with no retry affordance. The operator
 * could only recover by leaving the tab and coming back, which may or may
 * not re-trigger a successful fetch.
 *
 * Fix: a "Retry" button inside the error banner that re-fires the catalog
 * fetch without navigating away. This spec forces the first fetch to fail
 * via route interception, confirms the editor is replaced by the error +
 * Retry button (not silently broken with no feedback), then lets the
 * retried fetch succeed and confirms the real editor (and its reserved-
 * word help-info path) becomes usable again — all without a page reload.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const CATALOG_PATH = '**/api/admin/grammar/tokens*';

test.describe('ConsoleTab — catalog fetch failure shows a Retry button, not a dead end', () => {
  test('failed catalog fetch shows the error + Retry button with the editor hidden; Retry recovers it', async ({ page }) => {
    await loginAsAdmin(page);

    let shouldFail = true;
    await page.route(CATALOG_PATH, async (route) => {
      if (shouldFail) {
        await route.abort('failed');
      } else {
        await route.continue();
      }
    });

    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const errBanner = page.locator('.console-agent-cli-err');
    const retryBtn = page.locator('.console-agent-cli-retry');
    const textarea = page.locator('textarea.agent-cli-textarea');

    await expect(errBanner).toBeVisible({ timeout: 10_000 });
    await expect(errBanner).toContainText('CLI catalog unavailable');
    await expect(retryBtn).toBeVisible();
    await expect(retryBtn).toHaveText(/retry/i);
    // The editor must be fully replaced, not rendered-but-broken underneath.
    await expect(textarea).toHaveCount(0);

    // Let the retried fetch succeed.
    shouldFail = false;
    await retryBtn.click();

    await expect(errBanner).toHaveCount(0, { timeout: 10_000 });
    await expect(textarea).toBeVisible({ timeout: 10_000 });
  });

  test('Retry disables itself and shows "Retrying…" while a retry is in flight', async ({ page }) => {
    await loginAsAdmin(page);

    // Keep the retry request pending indefinitely (never resolve/abort) so
    // the test can observe the button's in-flight state deterministically,
    // instead of racing a real round-trip. fetchAgentCatalog() fires 3
    // requests in parallel (condition/notify/action) per attempt — the
    // first 3 calls (the initial mount) abort, everything after that
    // (the retry's own 3 calls) pends until released.
    let releaseRetry = () => {};
    const retryPending = new Promise((resolve) => { releaseRetry = resolve; });
    let callCount = 0;
    await page.route(CATALOG_PATH, async (route) => {
      callCount += 1;
      if (callCount <= 3) {
        await route.abort('failed');
        return;
      }
      await retryPending;
      await route.continue();
    });

    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const retryBtn = page.locator('.console-agent-cli-retry');
    await expect(retryBtn).toBeVisible({ timeout: 10_000 });

    await retryBtn.click();

    // While the retried fetch is still pending, the button must disable
    // itself and show feedback — this IS the guard
    // (`_agentCliCatalogRetrying`) that prevents a second overlapping fetch.
    await expect(retryBtn).toBeDisabled({ timeout: 5_000 });
    await expect(retryBtn).toHaveText(/retrying/i);

    releaseRetry();
    await expect(page.locator('textarea.agent-cli-textarea')).toBeVisible({ timeout: 10_000 });
  });
});
