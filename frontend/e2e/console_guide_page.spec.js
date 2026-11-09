/**
 * console_guide_page.spec.js
 *
 * In-app operator guide for the Console CLI grammar. Moved 2026-10
 * (Phase 2 Sprint B — "Console retirement") from
 *   frontend/src/routes/(algo)/console/guide/+page.svelte
 * to
 *   frontend/src/routes/(algo)/automation/guide/+page.svelte
 * — the old `/console/guide` URL now redirects there
 * (frontend/src/routes/(algo)/console/guide/+page.js).
 *
 * Covers:
 *   1. The guide page renders (via the old /console/guide URL, through
 *      the redirect) and shows the expected section headers (Orders,
 *      Agents, Event agents, Tokens, Meta tags, Quick reference). The
 *      Brackets section was removed 2026-11 (Track 3, T3.4, Bracket/
 *      Template/GTT removal) — `bracket(...)` is a hard compile error
 *      now, so there is nothing left to document.
 *   2. Clicking the Guide link on Automation's Console tab navigates to
 *      /automation/guide successfully.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;

test.describe('Console CLI guide page', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('renders with all expected section headers', async ({ page }) => {
    await page.goto('/console/guide');
    await expect(page.locator('h1.page-title-chip', { hasText: 'Console CLI Guide' }))
      .toBeVisible({ timeout: TIMEOUT });

    const expectedHeadings = [
      /Orders/,
      /^Agents/,
      /Event agents/,
      /^Tokens/,
      /Meta tags/,
      /Quick reference/,
    ];
    for (const re of expectedHeadings) {
      await expect(page.locator('h2.section-heading', { hasText: re }).first())
        .toBeVisible({ timeout: TIMEOUT });
    }

    // Brackets section removed 2026-11 (Track 3, T3.4, Bracket/Template/
    // GTT removal) — must not reappear.
    await expect(page.locator('h2.section-heading', { hasText: /Brackets/ })).toHaveCount(0);

    // Spot-check one real, confirmed-working example statement renders
    // verbatim (regression guard against accidental rewording that
    // would desync the guide from the actual grammar).
    await expect(page.locator('pre.cg-code', {
      hasText: 'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)',
    }).first()).toBeVisible();
  });

  test('Guide link on the Console tab navigates to /automation/guide', async ({ page }) => {
    await page.goto('/automation?tab=console');
    // Guide link relocated (2026-10) from its own row inside ConsoleTab to
    // the page-level CardHeader sharing the Console/Agents tab row — same
    // `.console-guide-btn` class, now found via `a.console-guide-btn`.
    const guideLink = page.locator('a.console-guide-btn');
    await expect(guideLink).toBeVisible({ timeout: TIMEOUT });
    await guideLink.click();
    await expect(page).toHaveURL(/\/automation\/guide$/, { timeout: TIMEOUT });
    await expect(page.locator('h1.page-title-chip', { hasText: 'Console CLI Guide' }))
      .toBeVisible({ timeout: TIMEOUT });
  });
});
