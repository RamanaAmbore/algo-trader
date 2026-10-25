/**
 * market_closed_color_consistency.spec.js
 *
 * 2026-10-24 — operator report: "market closed, connection chip colors,
 * popup window text color, refresh button color, M color, popup window
 * filled in circle color to have same color to be consistent."
 *
 * Before this fix, the app carried TWO different colours for "the market
 * is closed":
 *   - sky (--algo-sky, #7dd3fc) — BrokerHealthBadge's ticker-idle banner
 *     text/dot/Status badge + the navbar .broker-chip-idle (shipped
 *     earlier the same session, commits 0410c7e3 / a252883c).
 *   - slate (--algo-dim/#94a3b8) — RefreshButton's `.rf-mkt-closed` and
 *     AlgoTimestamp's "M" market-status pill (+ its InfoHint popup
 *     accent), both pre-dating the ticker-idle work.
 *
 * This spec locks every one of those surfaces onto the SAME sky token
 * going forward, mocking both the broker-health endpoint (ticker_idle)
 * and the market-status endpoint (both segments closed) so the check
 * doesn't depend on real wall-clock market state.
 *
 * Per-item mapping back to the operator's 5-item list (see handback
 * report for the full per-item audit):
 *   1. "connection chip colors"          -> .broker-chip-idle (navbar)
 *   2. "popup window text color"         -> .bh-idle-banner-label (BrokerHealthBadge)
 *   3. "refresh button color"            -> .rf-btn / .rf-mkt-closed (RefreshButton)
 *   4. "M color"                         -> algo-ts-market-badge-any (AlgoTimestamp)
 *   5. "popup window filled in circle"   -> .bh-dot-idle (BrokerHealthBadge)
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(120_000);
const WAIT_TIMEOUT = 30_000;
const SKY_RGB = 'rgb(125, 211, 252)'; // --algo-sky / #7dd3fc

/** @param {import('@playwright/test').Page} P */
async function mockClosedMarketAndIdleTicker(P) {
  const now = new Date().toISOString();
  await P.route('**/api/market/status', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      nse_open: false,
      mcx_open: false,
      any_open: false,
      is_holiday: true,
      checked_at: now,
    }),
  }));
  await P.route('**/api/admin/broker-health', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      accounts: [
        {
          account: 'ZG0790', broker: 'kite', state: 'green',
          reason: 'healthy', last_good_at: now, last_check_at: now,
          is_active_ticker: true, circuit_state: 'closed',
          consecutive_fail_count: 0, circuit_open_until: null,
          circuit_breaker_enabled: false,
        },
      ],
      groww_entitlement_denied: {},
      primary_market_data_account: 'ZG0790',
      ticker_idle: true,
      ticker_idle_reason: 'market calendar confirms closed today — ticker not connected by design',
    }),
  }));
  await P.route('**/api/admin/brokers', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([
      { account: 'ZG0790', loaded: true, broker_id: 'zerodha_kite', is_active: true },
    ]),
  }));
}

for (const viewport of [
  { width: 1440, height: 900, label: 'desktop' },
  { width: 375, height: 800, label: 'mobile' },
]) {
  test.describe(`market-closed colour parity (${viewport.label})`, () => {
    test.use({ viewport });

    test('chip, RefreshButton, and the M badge all resolve to the same sky colour as the broker-idle popup', async ({ page }) => {
      await mockClosedMarketAndIdleTicker(page);
      await loginAsAdmin(page);
      await page.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: WAIT_TIMEOUT });

      // 1. Connection chip (navbar) — already sky pre-fix; guard it stays so.
      // The layout renders TWO `.broker-chip` buttons (a desktop instance
      // + a mobile mirror, +layout.svelte ~L1250/~L1381), toggled by a CSS
      // breakpoint rather than JS — `:visible` picks whichever one the
      // current viewport actually shows, instead of always landing on the
      // first (desktop) DOM node, which is `display:none` on a narrow
      // viewport.
      const chip = page.locator('button.broker-chip:visible').first();
      await chip.waitFor({ state: 'visible', timeout: WAIT_TIMEOUT });
      await expect(chip).toHaveClass(/broker-chip-idle/);
      const chipColor = await chip.evaluate((el) => getComputedStyle(el).color);
      expect(chipColor, 'navbar connection chip must be sky while ticker-idle').toBe(SKY_RGB);

      // 3. RefreshButton — was slate, now sky (this fix).
      const refreshBtn = page.locator('.rf-btn').first();
      await refreshBtn.waitFor({ state: 'visible', timeout: WAIT_TIMEOUT });
      await expect(refreshBtn).toHaveClass(/rf-mkt-closed/);
      const refreshColor = await refreshBtn.evaluate((el) => getComputedStyle(el).color);
      expect(refreshColor, 'RefreshButton market-closed colour must match the broker-idle sky').toBe(SKY_RGB);

      // 4. AlgoTimestamp "M" badge — was slate, now sky (this fix).
      const mBadge = page.locator('[data-testid="algo-ts-market-badge-any"]').first();
      await mBadge.waitFor({ state: 'visible', timeout: WAIT_TIMEOUT });
      await expect(mBadge).toHaveClass(/ats-mkt-closed/);
      await expect(mBadge).toHaveText('M');
      const mColor = await mBadge.evaluate((el) => getComputedStyle(el).color);
      expect(mColor, 'AlgoTimestamp "M" market-closed colour must match the broker-idle sky').toBe(SKY_RGB);

      // 2 & 5. BrokerHealthBadge popup — text + filled dot, already sky
      // pre-fix; guard them so a future edit can't silently drift.
      await chip.click();
      const modal = page.locator('.bh-modal').first();
      await modal.waitFor({ state: 'visible', timeout: WAIT_TIMEOUT });

      const bannerLabel = modal.locator('.bh-idle-banner-label').first();
      await expect(bannerLabel).toBeVisible();
      const bannerColor = await bannerLabel.evaluate((el) => getComputedStyle(el).color);
      expect(bannerColor, 'popup idle-banner text must be sky').toBe(SKY_RGB);

      const idleDot = modal.locator('.bh-dot-idle').first();
      await expect(idleDot).toBeVisible();
      const dotColor = await idleDot.evaluate((el) => getComputedStyle(el).backgroundColor);
      expect(dotColor, 'popup filled idle dot must be sky').toBe(SKY_RGB);

      await page.locator('.bh-close').first().click();
      await modal.waitFor({ state: 'hidden', timeout: 3000 }).catch(() => {});
    });
  });
}
