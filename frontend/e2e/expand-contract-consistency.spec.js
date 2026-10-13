/**
 * Expand/Contract Button Consistency — Dashboard Activity + Agent Activity
 * cards.
 *
 * Header button-group standardization (2026-10-13):
 *   - Part 1 — Collapse removed project-wide (CollapseButton deleted from
 *     CardControls.svelte; isCollapsed/cardId-for-collapse/class:is-collapsed
 *     wiring stripped from every call site, including both cards here).
 *   - Part 3 — Dashboard-specific: Fullscreen removed too (unlike most
 *     other migrated pages, which keep it).
 *
 * This file used to assert Collapse/Fullscreen toggle behavior + structural
 * parity between the two cards (collapse-btn/fs-btn present, aria-labels,
 * localStorage persistence). None of that is reachable any more — both
 * controls are gone, not just hidden — so this is now a pure removal-
 * regression guard: neither card renders a Collapse or Fullscreen button,
 * and neither can carry the is-collapsed/fs-card-on classes the old
 * bindings used to toggle.
 *
 * Auth: requires admin credentials (set PLAYWRIGHT_USER + PLAYWRIGHT_PASS,
 * or use cached token).
 * Execution: `cd frontend && npx playwright test e2e/expand-contract-consistency.spec.js`
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('Expand/Contract Button Consistency — Dashboard Activity + Agent cards', () => {

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('Activity card — no Collapse or Fullscreen button, card body always visible', async ({ page }) => {
    await page.goto('/dashboard');
    const activityCard = page.locator('section.dash-activity');
    await expect(activityCard).toBeVisible({ timeout: 30_000 });

    const cardBody = activityCard.locator('.card-body').first();
    await expect(cardBody).toBeVisible({ timeout: 15_000 });

    await expect(activityCard.locator('button.collapse-btn')).toHaveCount(0);
    await expect(activityCard.locator('button.fs-btn')).toHaveCount(0);
    await expect(activityCard.locator('button.default-btn')).toHaveCount(0);
    await expect(activityCard).not.toHaveClass(/is-collapsed/);
    await expect(activityCard).not.toHaveClass(/fs-card-on/);

    // Regression guard — Download still renders on this card, now
    // routed via LogPanel's own header (unaffected by the removal).
    await expect(activityCard.locator('.grid-download-btn, .lp-fs-btn')).not.toHaveCount(0);
  });

  test('Agent activity card — no Collapse or Fullscreen button, card body always visible', async ({ page }) => {
    await page.goto('/dashboard');
    const agentCard = page.locator('section.dash-agent');
    await expect(agentCard).toBeVisible({ timeout: 30_000 });

    const cardBody = agentCard.locator('.card-body').first();
    await expect(cardBody).toBeVisible({ timeout: 15_000 });

    await expect(agentCard.locator('button.collapse-btn')).toHaveCount(0);
    await expect(agentCard.locator('button.fs-btn')).toHaveCount(0);
    await expect(agentCard.locator('button.default-btn')).toHaveCount(0);
    await expect(agentCard).not.toHaveClass(/is-collapsed/);
    await expect(agentCard).not.toHaveClass(/fs-card-on/);
  });

  test('Neither card persists a collapse state to localStorage any more', async ({ page }) => {
    await page.goto('/dashboard');
    await expect(page.locator('section.dash-activity')).toBeVisible({ timeout: 30_000 });
    await expect(page.locator('section.dash-agent')).toBeVisible({ timeout: 30_000 });

    const username = await page.evaluate(() => {
      const auth = sessionStorage.getItem('ramboq_user');
      return auth ? JSON.parse(auth).username : 'demo';
    });
    const stored = await page.evaluate((key) => localStorage.getItem(key),
      `ramboq.collapse.${username}.dash-activity`);
    expect(stored).toBeNull();
  });

});
