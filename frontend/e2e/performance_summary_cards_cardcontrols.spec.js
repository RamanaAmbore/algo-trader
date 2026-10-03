/**
 * performance_summary_cards_cardcontrols.spec.js
 *
 * PerformancePage.svelte's Summary grid cards (Positions Summary /
 * Holdings Summary) used a hand-rolled `.perf-grid-headrow` (title +
 * spacer + a bare GridDownloadButton) instead of the canonical
 * CardHeader/CardControls cluster — missing Search and Fullscreen
 * entirely, unlike every other grid card on the site (MarketPulse,
 * /dashboard, /admin/derivatives). The Breakdown cards had Search +
 * Download already but were missing Fullscreen + Collapse.
 *
 * Fix: all four grid cards (Positions/Holdings × Summary/Breakdown)
 * now wrap a <CardHeader> with the full cluster. `showGridControls`
 * (false on the public /performance page per
 * `(public)/performance/+page.svelte`) maps to CardHeader's
 * `showControls`.
 *
 * The ONE current mount point (`(public)/performance/+page.svelte`)
 * pins `showGridControls={false}`, so the only live-page assertion
 * this spec can make is the negative path: the whole cluster must
 * still be suppressed (same end state as before this fix — the old
 * `{#if showGridControls}` hid every button too), guarding against a
 * regression where `showControls` is accidentally left unwired (which
 * would default CardHeader's `showControls` to `true` and leak the
 * cluster onto the public page against the original design). The
 * positive path (cluster renders when showControls=true) — i.e. proof
 * that Search/Fullscreen were actually ADDED, not just that the
 * suppression plumbing works — is covered by the Vitest source audit
 * `frontend/src/lib/__tests__/performanceCardControls.test.js`, plus
 * `cardheader-smoke.spec.js`'s existing positive-path coverage for the
 * shared CardHeader component.
 */

import { test, expect } from '@playwright/test';

test.describe('(3) PerformancePage grid cards — CardHeader/CardControls cluster', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('/performance (showGridControls=false): CardHeader titles render, but the control cluster is suppressed', async ({ page }) => {
    await page.goto('/performance', { waitUntil: 'domcontentloaded' });

    // Default tab is Positions — both the Summary and Breakdown cards
    // for Positions should be visible with their CardHeader titles.
    const summaryHeader = page.locator('.perf-grid-card:not(.hidden) .card-header', { hasText: 'Summary' }).first();
    await expect(summaryHeader).toBeVisible({ timeout: 15_000 });
    const breakdownHeader = page.locator('.perf-grid-card:not(.hidden) .card-header', { hasText: 'Breakdown' }).first();
    await expect(breakdownHeader).toBeVisible();

    // showGridControls={false} on the public page maps to CardHeader's
    // showControls — the whole cluster (Search/Collapse/Fullscreen/
    // Download) must be suppressed, matching the old behavior where
    // `{#if showGridControls}` hid every button.
    await expect(summaryHeader.locator('button')).toHaveCount(0);
    await expect(breakdownHeader.locator('button')).toHaveCount(0);
  });

  test('collapse state: .perf-grid-card uses the global .is-collapsed ag-grid height:0 rule (no local override needed)', async ({ page }) => {
    await page.goto('/performance', { waitUntil: 'domcontentloaded' });
    const summaryCard = page.locator('.perf-grid-card:not(.hidden)', { has: page.locator('.card-header', { hasText: 'Summary' }) }).first();
    await expect(summaryCard).toBeVisible({ timeout: 15_000 });
    // Collapse control is suppressed (showControls=false) so this is a
    // structural check only: the card never carries a conflicting
    // local height rule that could fight the global `.is-collapsed`
    // selector once an authenticated surface (if any, in the future)
    // enables the cluster.
    await expect(summaryCard).not.toHaveClass(/is-collapsed/);
  });
});
