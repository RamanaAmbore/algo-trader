/**
 * Mobile single-line layout — PositionStrip + P&L summary card.
 *
 * The PositionStrip and the PnlAnalysis summary KV row both have to
 * fit on one horizontal line at every viewport in the project matrix
 * (chromium-desktop / mobile-portrait 360×800 / mobile-landscape
 * 800×360). Run with --project=mobile-portrait or mobile-landscape
 * for the meaningful checks; it also passes on desktop trivially.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;

/** Assert every locator in the list shares a vertical position
 *  (within `tol` px of the first), proving they're on one line. */
async function assertSingleRow(locators, tol = 4) {
  const ys = [];
  for (const loc of locators) {
    const box = await loc.boundingBox();
    if (!box) throw new Error('boundingBox null — element not laid out');
    ys.push(box.y);
  }
  const firstY = ys[0];
  for (const y of ys) {
    expect(Math.abs(y - firstY), `expected y within ${tol}px of ${firstY}, got ${y}`).toBeLessThanOrEqual(tol);
  }
}

test.describe('Mobile strip — single line', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('PositionStrip 7 chips on one row', async ({ page }) => {
    await page.goto('/dashboard');
    // Wait for the strip to mount; it self-fetches /api/positions and
    // /api/holdings, then renders the chip row regardless of data.
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });
    const chips = page.locator('.ps-strip .ps-agg');
    // The strip ships exactly 7 keys: P / M / C / P∆ / HD∆ / H∆ / H.
    await expect(chips).toHaveCount(7);
    const all = await chips.all();
    await assertSingleRow(all);
  });

  test('P&L summary card — 4 KV cells on one row', async ({ page }) => {
    await page.goto('/dashboard?tab=pnl');
    // The summary-row only mounts when /api/pnl/analysis returns
    // data. Wait briefly; if no snapshots exist on this server,
    // skip rather than fail — this is a layout test, not a data
    // conformance test.
    const card = page.locator('.summary-row').first();
    try {
      await card.waitFor({ state: 'visible', timeout: 8_000 });
    } catch (_) {
      test.skip(true, 'no P&L data — summary card did not mount');
    }
    const kv = page.locator('.summary-row .kv');
    // Four KV cells: P% / vN / B↑ / W↓ — the summary-meta sibling
    // doesn't carry the .kv class so it doesn't count.
    await expect(kv).toHaveCount(4);
    const all = await kv.all();
    await assertSingleRow(all);
  });
});
