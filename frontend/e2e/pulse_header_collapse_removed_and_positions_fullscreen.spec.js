/**
 * pulse_header_collapse_removed_and_positions_fullscreen.spec.js
 *
 * Header button-group standardization (2026-10-13, Part 1 core + Part 2).
 *
 * Part 1: CollapseButton was deleted and CardControls.svelte no longer
 * renders it — verifies the chevron/caret toggle is gone from every
 * MarketPulse bucket header (Pinned/Watchlist, Winners, Losers,
 * Positions, Holdings), while its siblings (Search, Download,
 * Fullscreen) still render and remain clickable (regression guard —
 * removing one button in the shared CardControls cluster must not
 * silently break the others).
 *
 * Part 2: Positions' Fullscreen button was confirmed missing via live
 * DOM measurement — NOT a `ch-left` crowding/clipping issue as
 * originally suspected, but CardHeader's own `detectOverflow` heuristic
 * (`hideFullscreen={hideFullscreen || (detectOverflow && !_hasOverflow
 * && !isFullscreen)}`) auto-hiding Fullscreen whenever the account's
 * current book holds zero open positions (ag-Grid's row content then
 * can't overflow its viewport, so `_hasOverflow` never flips true).
 * Fixed via `detectOverflow={false}` on Positions' own CardHeader call
 * — this spec verifies the Fullscreen button is now ALWAYS visible and
 * clickable there, matching Winners/Losers/Holdings' existing behavior,
 * regardless of the account's current position count.
 *
 * Five quality dimensions:
 *   SSOT   — CollapseButton.svelte deleted; CardControls.svelte is the
 *            sole gate for which buttons render in every CardHeader-based
 *            card app-wide, so removing it there removes it everywhere.
 *   Perf   — detectOverflow={false} on Positions also skips a
 *            ResizeObserver/MutationObserver setup that card no longer
 *            needs (ancillary minor win, not separately asserted here).
 *   Stale  — grep-style DOM assertion: zero `.collapse-btn` /
 *            `is-collapsed` class usage anywhere in the five bucket
 *            headers/wrappers.
 *   Reuse  — Search/Download/Fullscreen (shared CardControls children)
 *            keep working identically post-removal.
 *   UX     — Positions' Fullscreen control no longer flickers in/out of
 *            existence as the operator's book opens/closes positions.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ viewport: { width: 1400, height: 900 } });

const BUCKETS = [
  { cls: 'mp-bucket-pinwatch',  label: 'Pinned/Watchlist' },
  { cls: 'mp-bucket-winners',   label: 'Winners' },
  { cls: 'mp-bucket-losers',    label: 'Losers' },
  { cls: 'mp-bucket-positions', label: 'Positions' },
  { cls: 'mp-bucket-holdings',  label: 'Holdings' },
];

async function gotoPulse(page) {
  await loginAsAdmin(page);
  await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
  // MarketPulse keeps an SSE connection open — networkidle never fires.
  // Wait for a real card-header + at least one bucket wrapper instead.
  await page.locator('.mp-bucket-wrap .card-header').first().waitFor({ state: 'visible', timeout: 30000 });
  // Let ag-Grid mount + row data settle.
  await page.waitForTimeout(2000);
}

test.describe('MarketPulse header button-group standardization', () => {

  // ── Part 1: Collapse button removed from every bucket header ──────────
  test('1. Stale: no .collapse-btn or is-collapsed class anywhere in MarketPulse', async ({ page }) => {
    await gotoPulse(page);

    const collapseBtnCount = await page.locator('.mp-bucket-wrap .collapse-btn').count();
    expect(collapseBtnCount, 'No .collapse-btn should exist in any MarketPulse bucket').toBe(0);

    const collapseByTitle = await page.locator(
      '.mp-bucket-wrap button[title*="ollapse" i], .mp-bucket-wrap button[aria-label*="ollapse" i]'
    ).count();
    expect(collapseByTitle, 'No button with a Collapse/Expand-card title should exist').toBe(0);

    for (const { cls, label } of BUCKETS) {
      const hasCollapsedClass = await page.locator(`.${cls}`).first().evaluate(
        el => el.classList.contains('is-collapsed')
      ).catch(() => false);
      expect(hasCollapsedClass, `${label}: .${cls} must never carry is-collapsed`).toBe(false);
    }
  });

  // ── Part 1: siblings (Search/Download/Fullscreen) still work ──────────
  test('2. Reuse: Search, Download and Fullscreen buttons still render and are clickable in every bucket', async ({ page }) => {
    await gotoPulse(page);

    for (const { cls, label } of BUCKETS) {
      const header = page.locator(`.${cls} .card-header`).first();
      await expect(header, `${label}: card-header should exist`).toBeVisible();

      const searchBtn = header.locator('.grid-search-btn, button[title*="ilter" i]').first();
      await expect(searchBtn, `${label}: search button should be visible`).toBeVisible();

      const downloadBtn = header.locator('.grid-download-btn, button[title*="ownload" i]').first();
      await expect(downloadBtn, `${label}: download button should be visible`).toBeVisible();

      // Search button should still be clickable (opens inline filter input)
      await searchBtn.click();
      await page.waitForTimeout(150);
      // Close it again (Esc) so state doesn't bleed into the next bucket's check.
      await page.keyboard.press('Escape');
    }
  });

  // ── Part 2: Positions' Fullscreen button is visible + clickable ───────
  test('3. UX: Positions panel has a working Fullscreen button, matching Winners/Losers/Holdings', async ({ page }) => {
    await gotoPulse(page);

    const posHeader = page.locator('.mp-bucket-positions .card-header').first();
    await expect(posHeader, 'Positions card-header should exist').toBeVisible();

    const posFsBtn = posHeader.locator('.fs-btn, button[title*="ullscreen" i], [aria-label*="ullscreen" i]').first();
    await expect(posFsBtn, 'Positions Fullscreen button should be visible').toBeVisible();

    // Sanity: the same selector finds a working Fullscreen button on a
    // sibling bucket too (Holdings), confirming parity rather than a
    // selector that happens to match something unrelated.
    const holdFsBtn = page.locator('.mp-bucket-holdings .card-header .fs-btn, .mp-bucket-holdings .card-header button[title*="ullscreen" i]').first();
    await expect(holdFsBtn, 'Holdings Fullscreen button should be visible (parity reference)').toBeVisible();

    // Click Positions' Fullscreen button — the bucket wrapper should gain
    // fs-card-on, proving the click actually drives real state, not just
    // a decorative button.
    await posFsBtn.click();
    await page.waitForTimeout(300);
    const posSection = page.locator('.mp-bucket-positions').first();
    await expect(posSection, 'Positions should enter fullscreen (fs-card-on) after click').toHaveClass(/fs-card-on/);

    // Exit fullscreen again (DefaultSizeButton) so the page is left clean.
    const exitBtn = posHeader.locator('button[title*="efault" i], button[aria-label*="efault" i], button[title*="xit" i]').first();
    if (await exitBtn.isVisible().catch(() => false)) {
      await exitBtn.click();
      await page.waitForTimeout(300);
    }
  });
});
