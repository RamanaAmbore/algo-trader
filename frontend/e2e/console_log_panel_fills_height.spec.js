/**
 * console_log_panel_fills_height.spec.js
 *
 * 2026-10-18 fix — operator: "in console, the log panel should use all
 * the available vertical space."
 *
 * automation/+page.svelte's bottom <ActivityLogSurface> was pinned to a
 * fixed `heightClass="h-[50vh]"` — a leftover from a much older,
 * pre-refactor layout (git history: "Algo log panel: max-h -> h-[50vh]
 * for consistency with Orders/Terminal", back when the page rendered a
 * raw <pre class="log-panel"> directly, long before the
 * ActivityLogSurface/LogPanel component existed). The component's own
 * default (`flex-1 min-h-0`) already implements "fill the available
 * space" correctly — the page's `.algo-content` ancestor is a flex
 * column specifically so descendants can opt into this (see its own
 * CSS comment) — the explicit override on this one page was just never
 * updated to match. Fix: drop the override, let the default apply.
 *
 * Run:
 *   npx playwright test e2e/console_log_panel_fills_height.spec.js
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('Automation Console — bottom log panel fills available vertical space', () => {
  test('log panel height is NOT capped at 50% of viewport height, and no page overflow is introduced', async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await loginAsAdmin(page);
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1500);

    const panel = page.locator('.log-panel').first();
    await expect(panel).toBeVisible({ timeout: 20_000 });
    const box = await panel.boundingBox();
    // Old fixed height was exactly 50vh = 450px at a 900px viewport.
    // The panel must now be meaningfully taller than that.
    expect(box.height).toBeGreaterThan(500);

    // Filling available space must not create a page-level scrollbar —
    // body.scrollHeight should match the viewport's clientHeight.
    const scrollHeight = await page.evaluate(() => document.body.scrollHeight);
    const clientHeight = await page.evaluate(() => document.documentElement.clientHeight);
    expect(Math.abs(scrollHeight - clientHeight)).toBeLessThan(5);
  });
});
