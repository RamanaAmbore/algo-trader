/**
 * Regression spec: Winners and Losers grids must be visible after a
 * cold-load of /pulse, including when sessionStorage carries stale tokens
 * from an older build.
 *
 * Root cause (2026-07-03):
 *   Two independent bugs caused both grids to silently disappear:
 *
 *   BUG 1 — Frontend (fixed in d4642136):
 *     MarketPulse.svelte stored 'src:movers' in sessionStorage under the
 *     pre-split 6-grid layout. After the Winners/Losers split, the token
 *     'src:movers' was pruned by the _availableSourceValues $effect because
 *     it no longer existed — only 'src:winners' and 'src:losers' were valid.
 *     The migration flatMap in onMount was added to translate 'src:movers' →
 *     ['src:winners', 'src:losers'] before the prune effect ran.
 *
 *   BUG 2 — Backend (fixed in this commit):
 *     _fetch_instruments() in instruments.py used brokers[0] from
 *     all_brokers(). When RAMBOQ_USE_CONN_SERVICE=1, the first account is
 *     sometimes Dhan (DH6847). Dhan instruments() returns dicts with
 *     'security_id'/'exchange_segment' field names — no 'instrument_type' or
 *     'name'. So inst.get("instrument_type", "") returned "" for all 155k rows,
 *     _underlyings_cache stayed empty, and movers returned an empty list.
 *     Fix: filter _loaded_accounts() to kite_accts (broker_id in
 *     {"zerodha_kite", "kite"}) before calling get_broker().
 *
 * Five quality dimensions:
 *   SSOT   — showWinners / showLosers derived correctly from selectedShow
 *   Perf   — /pulse lands and winners+losers both visible within 30 s
 *   Stale  — no stale 'src:movers' sessionStorage token survives after hydration
 *   Reuse  — fixture mock reused from movers_tab_coverage.spec.js pattern
 *   UX     — both .mp-bucket-winners and .mp-bucket-losers are visible with
 *             ≥ 1 row each; neither is hidden or has display:none
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

async function signIn(page) {
  for (const creds of [
    { user: process.env.PLAYWRIGHT_USER || 'ambore', pass: process.env.PLAYWRIGHT_PASS || 'admin1234' },
    { user: 'rambo', pass: process.env.PLAYWRIGHT_PASS || 'admin1234' },
  ]) {
    try {
      await loginAsAdmin(page, creds);
      return creds.user;
    } catch (_) { /* try next */ }
  }
  throw new Error('Could not sign in');
}

/** Wire a minimal movers fixture with winners AND losers. */
async function mockMovers(page) {
  const make = (sym, i, pct) => ({
    tradingsymbol:  sym,
    exchange:       'NSE',
    last_price:     pct > 0 ? 1050 + i * 20 : 950 - i * 20,
    previous_close: 1000 + i * 20,
    change_pct:     pct,
    peak_pct:       Math.abs(pct) + 0.2,
    sticky:         false,
  });

  const movers = [
    // winners (change_pct > 0)
    make('RELIANCE',  0,  2.5),
    make('TCS',       1,  1.9),
    make('NIFTY 50',  2,  1.7),
    // losers (change_pct < 0)
    make('INFY',      3, -2.1),
    make('WIPRO',     4, -1.8),
    make('HCLTECH',   5, -1.6),
  ];

  await page.route('**/api/watchlist/movers', route =>
    route.fulfill({
      status:      200,
      contentType: 'application/json',
      body: JSON.stringify({
        movers,
        threshold_pct: 1.5,
        session_date:  new Date().toISOString().slice(0, 10),
        captured_at:   null,
      }),
    })
  );
}

test.describe('Movers Winners + Losers grids regression', () => {
  test('fresh load — both Winners and Losers grids visible with rows', async ({ page }) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width: 1440, height: 900 });

    await signIn(page);
    await mockMovers(page);

    // Clear any stale sessionStorage before navigating so the test starts clean.
    await page.addInitScript(() => {
      sessionStorage.removeItem('mp.selectedShow');
    });

    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    // SSOT: Winners bucket is present and visible.
    const winners = page.locator('.mp-bucket-winners').first();
    await expect(winners).toBeVisible({ timeout: 30_000 });

    // SSOT: Losers bucket is present and visible.
    const losers = page.locator('.mp-bucket-losers').first();
    await expect(losers).toBeVisible({ timeout: 30_000 });

    // Perf: rows appear within 60 s.
    await expect(
      page.locator('.mp-bucket-winners .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });

    await expect(
      page.locator('.mp-bucket-losers .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });

    // UX: count rows.
    const winCount = await page.locator('.mp-bucket-winners .ag-center-cols-container .ag-row').count();
    const loseCount = await page.locator('.mp-bucket-losers .ag-center-cols-container .ag-row').count();
    console.log(`[fresh] winners=${winCount} losers=${loseCount}`);

    expect(winCount,  'Winners grid must have ≥ 1 row').toBeGreaterThanOrEqual(1);
    expect(loseCount, 'Losers grid must have ≥ 1 row').toBeGreaterThanOrEqual(1);
  });

  test('stale sessionStorage src:movers → migrated to src:winners + src:losers', async ({ page }) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width: 1440, height: 900 });

    await signIn(page);
    await mockMovers(page);

    // Seed the OLD token that caused the regression.
    // This simulates a browser that had the pre-split build cached.
    await page.addInitScript(() => {
      sessionStorage.setItem(
        'mp.selectedShow',
        JSON.stringify(['src:movers'])   // ← old single token, now invalid
      );
    });

    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    // Stale: the old 'src:movers' token must NOT survive — both grids must render.
    const winners = page.locator('.mp-bucket-winners').first();
    const losers  = page.locator('.mp-bucket-losers').first();

    await expect(winners).toBeVisible({ timeout: 30_000 });
    await expect(losers).toBeVisible({ timeout: 30_000 });

    // Wait for rows in both.
    await expect(
      page.locator('.mp-bucket-winners .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });

    await expect(
      page.locator('.mp-bucket-losers .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });

    // Verify sessionStorage was updated by the migration to the new tokens.
    const stored = await page.evaluate(() => {
      const v = sessionStorage.getItem('mp.selectedShow');
      return v ? JSON.parse(v) : null;
    });
    console.log('[migration] sessionStorage after load:', stored);

    expect(Array.isArray(stored), 'sessionStorage must be an array').toBe(true);
    expect(
      stored.includes('src:winners'),
      'Migrated sessionStorage must contain src:winners'
    ).toBe(true);
    expect(
      stored.includes('src:losers'),
      'Migrated sessionStorage must contain src:losers'
    ).toBe(true);
    expect(
      stored.includes('src:movers'),
      'Migrated sessionStorage must NOT contain stale src:movers token'
    ).toBe(false);
  });

  test('mobile portrait — both grids visible stacked', async ({ page }) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width: 390, height: 844 });

    await signIn(page);
    await mockMovers(page);

    await page.addInitScript(() => {
      sessionStorage.removeItem('mp.selectedShow');
    });

    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    // On mobile both buckets stack vertically — both must be present.
    const winners = page.locator('.mp-bucket-winners').first();
    const losers  = page.locator('.mp-bucket-losers').first();

    await expect(winners).toBeVisible({ timeout: 30_000 });
    await expect(losers).toBeVisible({ timeout: 30_000 });

    await expect(
      page.locator('.mp-bucket-winners .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });

    await expect(
      page.locator('.mp-bucket-losers .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });

    const winCount  = await page.locator('.mp-bucket-winners .ag-center-cols-container .ag-row').count();
    const loseCount = await page.locator('.mp-bucket-losers .ag-center-cols-container .ag-row').count();
    console.log(`[mobile] winners=${winCount} losers=${loseCount}`);

    expect(winCount,  'Mobile: Winners grid ≥ 1 row').toBeGreaterThanOrEqual(1);
    expect(loseCount, 'Mobile: Losers grid ≥ 1 row').toBeGreaterThanOrEqual(1);
  });
});
