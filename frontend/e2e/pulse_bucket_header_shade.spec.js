/**
 * pulse_bucket_header_shade.spec.js — 2026-10-12, operator: "all panel
 * headers which have grids, have headers as same color as panel
 * background... slightly different shade for the header for emphasis.
 * apply it only in pulse page presently."
 *
 * MarketPulse.svelte's bucket panels (.mp-bucket-wrap — Positions,
 * Holdings, Gainers, Losers, Pinned/Watchlist) each wrap a CardHeader
 * whose `--ch-bg` custom property defaulted to `transparent`, so the
 * header bar visually inherited the panel body's own background
 * (--card-bg-elevated) with no distinction. Fixed by setting --ch-bg to
 * a subtle highlight layered over the SAME gradient, scoped to
 * .mp-bucket-wrap only (CardHeader.svelte itself is untouched — this is
 * Pulse-only for now, per the operator's own scoping instruction).
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
});
