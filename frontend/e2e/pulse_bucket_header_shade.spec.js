/**
 * pulse_bucket_header_shade.spec.js — 2026-10-12, operator: "all panel
 * headers which have grids, have headers as same color as panel
 * background... slightly different shade for the header for emphasis.
 * apply it only in pulse page presently." REVISED same day — operator:
 * "apply the header color scheme for other panels in other pages and
 * modals. this should use a common code for headers" — the Pulse-only
 * override (.mp-bucket-wrap's own --ch-bg) was removed and generalized
 * into `(algo)/+layout.svelte`'s `:global(body)` CardHeader-theming
 * block, the ONE shared default every CardHeader in the algo app
 * inherits. This file keeps its Pulse-specific assertions (still
 * correct — Pulse inherits the same default now) and adds one
 * assertion proving the generalization actually reaches a page outside
 * Pulse.
 *
 * Also covers: Pinned/Watchlist's own left-bar accent (operator: "in
 * pinned panel pulse add left vertical bar like other panels in the
 * pulse") — the other 4 bucket panels already had this via their
 * `.mp-bucket-label` span's `border-left`; Pinned lacked it since it
 * uses AlgoTabs instead of a plain label span.
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

test.describe('Pulse bucket panel header gets a distinct shade from the panel body', () => {
  test('Positions panel: .card-header background differs from .mp-bucket-wrap background', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const panel = page.locator('.mp-bucket-positions').first();
    await expect(panel).toBeVisible({ timeout: 20_000 });
    const header = panel.locator('.card-header').first();
    await expect(header).toBeVisible();

    const [panelBg, headerBg] = await Promise.all([
      panel.evaluate((el) => getComputedStyle(el).backgroundImage || getComputedStyle(el).backgroundColor),
      header.evaluate((el) => getComputedStyle(el).backgroundImage || getComputedStyle(el).backgroundColor),
    ]);

    expect(headerBg, 'header background must differ from the panel body background').not.toBe(panelBg);
    // Not transparent/none either — confirms a real background actually rendered.
    expect(headerBg).not.toBe('none');
  });

  test('every bucket panel (Positions/Holdings/Gainers/Losers/Pinned) gets the same distinct header treatment', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const buckets = ['positions', 'holdings', 'winners', 'losers', 'pinwatch'];
    for (const bucket of buckets) {
      const panel = page.locator(`.mp-bucket-${bucket}`).first();
      await expect(panel, `${bucket} panel must exist`).toBeVisible({ timeout: 20_000 });
      const header = panel.locator('.card-header').first();
      const headerBg = await header.evaluate((el) => getComputedStyle(el).backgroundImage);
      expect(headerBg, `${bucket} header must have a real background-image (not 'none')`).not.toBe('none');
    }
  });

  test('Pinned/Watchlist panel gets its own left-bar accent (AlgoTabs wrapper, same 3px treatment as the other 4 panels\' .mp-bucket-label)', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const leftBar = page.locator('.mp-bucket-pinwatch .mp-pinwatch-leftbar').first();
    await expect(leftBar).toBeVisible({ timeout: 20_000 });
    const borderLeft = await leftBar.evaluate((el) => getComputedStyle(el).borderLeftWidth);
    expect(parseFloat(borderLeft)).toBeGreaterThan(0);
  });
});

test.describe('CardHeader emphasis generalized beyond Pulse (operator: "apply the header color scheme for other panels in other pages and modals. this should use a common code for headers")', () => {
  test('a non-Pulse page (Automation → Console tab) also gets the shared header-shade default', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const header = page.locator('.console-card .card-header').first();
    await expect(header).toBeVisible({ timeout: 20_000 });
    const headerBg = await header.evaluate((el) => getComputedStyle(el).backgroundImage);
    expect(headerBg, 'Console tab header must inherit the shared (algo)/+layout.svelte default, not the old transparent one').not.toBe('none');
  });
});
