/**
 * pnl_analysis_algo_tabs_verify.spec.js
 *
 * Verification spec for a (confirmed-stale) audit claim: "PnlAnalysis.svelte
 * hand-rolls its own role=tab strip instead of using the shared AlgoTabs.svelte
 * component." Confirms the Range-breakdown tab strip (Segment / Account /
 * Symbol / Daily) inside PnlAnalysis renders via the canonical <AlgoTabs>
 * component (root class `.algo-tabs-strip`, per AlgoTabs.svelte), and that
 * clicking a second tab actually swaps the visible content.
 *
 * Host page: /dashboard — row-1 chart card, Performance sub-tab.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=http://localhost:5174 \
 *   npx playwright test pnl_analysis_algo_tabs_verify --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('PnlAnalysis range-breakdown tabs use canonical AlgoTabs', () => {
  test('tab strip renders via AlgoTabs and switching tabs changes content', async ({ page }) => {
    test.setTimeout(60_000);
    await loginAsAdmin(page);

    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    // Switch the dashboard's outer chart-card tab (NAV / Intraday /
    // Performance — itself an AlgoTabs instance) to Performance, which
    // mounts PnlAnalysis.
    const perfTab = page.getByRole('tab', { name: 'Performance' }).first();
    await expect(perfTab).toBeVisible({ timeout: 20_000 });
    await perfTab.click();

    // The PnlAnalysis breakdown strip is the AlgoTabs instance that
    // contains a 'Segment' tab button — unique among dashboard tab
    // strips, so this scoping works without relying on card-nesting
    // indices that could drift with future layout changes.
    const breakdownStrip = page.locator('.algo-tabs-strip').filter({
      has: page.getByRole('tab', { name: 'Segment' }),
    }).first();
    await expect(breakdownStrip).toBeVisible({ timeout: 20_000 });

    // Confirm all four expected tabs are present and rendered via the
    // shared AlgoTabs primitive (role=tab buttons inside .algo-tabs-strip,
    // not a hand-rolled .tab-strip/.tab-on markup).
    const segmentTab = breakdownStrip.getByRole('tab', { name: 'Segment' });
    const accountTab = breakdownStrip.getByRole('tab', { name: 'Account' });
    const symbolTab  = breakdownStrip.getByRole('tab', { name: 'Symbol' });
    const dailyTab   = breakdownStrip.getByRole('tab', { name: 'Daily' });
    await expect(segmentTab).toBeVisible();
    await expect(accountTab).toBeVisible();
    await expect(symbolTab).toBeVisible();
    await expect(dailyTab).toBeVisible();

    // Default tab is 'Segment' — aria-selected reflects canonical AlgoTabs
    // active-state wiring (no separate hand-rolled .tab-on class needed).
    await expect(segmentTab).toHaveAttribute('aria-selected', 'true');
    await expect(dailyTab).toHaveAttribute('aria-selected', 'false');

    // Capture the breakdown body's visible text while on Segment, then
    // switch to Daily — content must change (table headers differ:
    // Segment/Account/Symbol tabs render a 'Rows' column, Daily does not;
    // empty-state copy also differs — 'No data in range.' vs 'No daily
    // data in range.'). The body is the sibling content block directly
    // following the breakdown strip's containing card.
    const breakdownCard = breakdownStrip.locator('..');
    const beforeText = (await breakdownCard.innerText()).trim();

    await dailyTab.click();
    await expect(dailyTab).toHaveAttribute('aria-selected', 'true');
    await expect(segmentTab).toHaveAttribute('aria-selected', 'false');

    await expect.poll(async () => (await breakdownCard.innerText()).trim())
      .not.toBe(beforeText);

    // No leftover hand-rolled tab markup should exist for this specific
    // breakdown strip — it must be the canonical AlgoTabs button, not a
    // bespoke .tab/.tab-on element.
    await expect(breakdownStrip.locator('.tab-on')).toHaveCount(0);
  });
});
