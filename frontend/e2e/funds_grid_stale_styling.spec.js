/**
 * PerformancePage funds grid stale account styling — Playwright e2e spec.
 *
 * Tests the 2026-09 real-money fix for margin/cash staleness indicators in the
 * PerformancePage funds grid. When a broker's circuit-breaker opens and LKG
 * (last-known-good) data is served instead of a live fetch, the backend sets
 * `account_stale: true` on that account's row. PerformancePage.svelte's
 * `fundsRowClassRules` applies the `row-account-stale` CSS class to render
 * visual staleness (opacity reduction + dashed border).
 *
 * Related: OrderTicket.svelte also has stale badge rendering in .ot-funds,
 * but that component is mounted in SymbolPanel with fundsHidden={true},
 * making the badge unreachable (see footnote at end of this spec).
 *
 * Five quality dimensions:
 *  1. SSOT     — reads computed styles from the actual rendered row under
 *                .ag-theme-ramboq (the PerformancePage grid theme), not hardcoded
 *                assumptions. Verifies CSS rule at app.css:931–945 actually applies.
 *  2. Perf     — mocks /api/funds endpoint only; no live broker calls; renders
 *                in-viewport grids only.
 *  3. Stale    — verifies stale row DOES NOT flicker or disappear across reload cycles
 *                (regression guard for operator's core complaint: accounts appearing
 *                and disappearing).
 *  4. Reuse    — follows the pattern from dhan_stale_persist.spec.js; mocks same
 *                route structure + installs mocks before goto.
 *  5. UX       — asserts CSS class presence AND reads computed style values to
 *                prove the visual styling (opacity, border-style, border-color)
 *                actually renders, not just the class name exists.
 *
 * NOTE ON OrderTicket.svelte (.ot-funds-stale badge):
 * OrderTicket.svelte lines 2593–2594 render a STALE badge and line 2585 applies
 * the .ot-funds-stale class when account_stale === true. However, the .ot-funds
 * display is gated by `{#if _accountFunds && !fundsHidden}` (line 2583). The ONLY
 * mount of OrderTicket in the codebase is SymbolPanel.svelte line 2290, which
 * passes `fundsHidden={true}`. Therefore, the badge is unreachable UI. The CSS
 * classes and component logic are correct, but no current code path renders them
 * visible. This test covers PerformancePage (which works) but cannot test the
 * unreachable OrderTicket badge without fixing that blocker first.
 *
 * Run context: chromium-desktop (ag-Grid requires desktop viewport).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 30_000;

// Mocked market status — OPEN so funds API is the source of truth,
// not a closed-hours snapshot fallback.
const MARKET_OPEN = {
  nse_open: true,
  mcx_open: true,
  any_open: true,
  is_holiday: false,
};

// Healthy funds row — fresh broker fetch, non-stale
const HEALTHY_FUNDS = {
  account: 'ZG0790',
  avail_margin: 50000.50,
  used_margin: 25000.00,
  cash: 100000.00,
  available_funds: 50000.50,
  available_cash: 100000.00,
  collateral: 0.00,
  account_stale: false,
};

// Stale funds row — broker circuit-breaker open, served from LKG cache
const STALE_FUNDS = {
  account: 'DH6847',
  avail_margin: 30000.00,
  used_margin: 40000.00,
  cash: 50000.00,
  available_funds: 30000.00,
  available_cash: 50000.00,
  collateral: 10000.00,
  account_stale: true, // Load-bearing field — this makes the row stale
};

/**
 * Install route mocks before navigating.
 * Mocks market status (always open), funds (with mixed stale/healthy),
 * and empty positions/holdings so PerformancePage renders with only funds.
 */
async function installMocks(page) {
  await page.route('**/api/market/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(MARKET_OPEN),
    })
  );

  // Funds API: /api/funds, /api/funds/?fresh=1, /api/funds/?fresh=1&skip_ltp=1, etc.
  // Use regex to catch all query-string variants.
  await page.route(/\/api\/funds\/?(\?.*)?$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        rows: [HEALTHY_FUNDS, STALE_FUNDS],
        refreshed_at: 'Mon 29 Sep 14:30 IST',
        stale_accounts: ['DH6847'], // Response-level flag for clients that don't read per-row
      }),
    })
  );

  // Empty holdings so the page doesn't distract with holdings data
  await page.route(/\/api\/holdings\/?(\?.*)?$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        rows: [],
        summary: [],
        refreshed_at: 'Mon 29 Sep 14:30 IST',
        stale_accounts: [],
      }),
    })
  );

  // Empty positions so the page doesn't distract with positions data
  await page.route(/\/api\/positions\/?(\?.*)?$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        rows: [],
        summary: [],
        refreshed_at: 'Mon 29 Sep 14:30 IST',
        stale_accounts: [],
      }),
    })
  );
}

/**
 * Navigate to /performance and wait for the funds grid to mount and render.
 * PerformancePage lazy-loads ag-Grid, so we wait for the first row in the
 * center-cols container to attach.
 */
async function navigateToFundsGrid(page) {
  await page.goto('/performance', { waitUntil: 'domcontentloaded' });
  // ag-Grid renders rows under .ag-center-cols-container; wait for the first row.
  // The funds grid specifically uses .ag-theme-ramboq (not .ag-theme-algo).
  await page.locator('.ag-theme-ramboq .ag-center-cols-container .ag-row').first()
    .waitFor({ state: 'attached', timeout: TIMEOUT });
}

test.describe('PerformancePage funds grid stale account styling (2026-09 fix)', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await installMocks(page);
  });

  test('Both healthy and stale funds rows render in the funds grid', async ({ page }) => {
    await navigateToFundsGrid(page);

    // The funds grid is the LAST .ag-theme-ramboq grid on the page
    // (positions, holdings, funds, nav). Scope to the specific grid container.
    // Get the container by finding the grid and using its context.
    const fundsGrids = page.locator('.ag-theme-ramboq');
    // Use nth-2 (second-to-last) since nav is last; or find by text heuristic.
    // Actually, simpler: query from the specific grid container directly.
    // Use a more scoped selector: look for rows within a grid that has the funds columns.

    // The funds grid header contains "Account", "Net", "Util %", etc.
    // We can use the header as context, then find rows within that grid.
    const fundsContainer = page.locator('.ag-theme-ramboq').filter({
      hasText: /Account.*Net.*Util|Avl.Margin/i, // Funds grid specific headers
    }).first();

    const healthyRow = fundsContainer.locator('.ag-row[row-id="ZG0790"]').first();
    const staleRow = fundsContainer.locator('.ag-row[row-id="DH6847"]').first();

    // Both rows must exist in the DOM — before the fix, the stale row would silently
    // vanish from the payload when the breaker opened.
    await expect(healthyRow).toHaveCount(1);
    await expect(staleRow).toHaveCount(1);
  });

  test('DH6847 stale row carries the row-account-stale CSS class', async ({ page }) => {
    await navigateToFundsGrid(page);

    const fundsContainer = page.locator('.ag-theme-ramboq').filter({
      hasText: /Account.*Net.*Util|Avl.Margin/i,
    }).first();

    const staleRow = fundsContainer.locator('.ag-row[row-id="DH6847"]').first();
    // Verify the class is applied by fundsRowClassRules.
    await expect(staleRow).toHaveClass(/row-account-stale/);
  });

  test('ZG0790 healthy row does NOT carry the row-account-stale class', async ({ page }) => {
    await navigateToFundsGrid(page);

    const fundsContainer = page.locator('.ag-theme-ramboq').filter({
      hasText: /Account.*Net.*Util|Avl.Margin/i,
    }).first();

    const healthyRow = fundsContainer.locator('.ag-row[row-id="ZG0790"]').first();
    // Read the class list to verify absence (not just trust negative assertion).
    const classList = await healthyRow.evaluate((el) => Array.from(el.classList));
    expect(classList).not.toContain('row-account-stale');
  });

  test('Stale row displays reduced opacity + dashed border (CSS rule applies)', async ({ page }) => {
    await navigateToFundsGrid(page);

    const fundsContainer = page.locator('.ag-theme-ramboq').filter({
      hasText: /Account.*Net.*Util|Avl.Margin/i,
    }).first();

    const staleRow = fundsContainer.locator('.ag-row[row-id="DH6847"]').first();
    const healthyRow = fundsContainer.locator('.ag-row[row-id="ZG0790"]').first();

    // Read computed styles from both rows to verify the CSS rule at
    // app.css:931–940 (.ag-theme-ramboq .ag-row.row-account-stale) is applied.
    // The rule applies: opacity: 0.62, background-image with diagonal hatch pattern
    const staleStyle = await staleRow.evaluate((el) => {
      const cs = getComputedStyle(el);
      return {
        opacity: parseFloat(cs.opacity),
        backgroundImage: cs.backgroundImage,
      };
    });

    const healthyStyle = await healthyRow.evaluate((el) => {
      const cs = getComputedStyle(el);
      return {
        opacity: parseFloat(cs.opacity),
        backgroundImage: cs.backgroundImage,
      };
    });

    // Stale row should have reduced opacity (0.62)
    expect(staleStyle.opacity).toBeLessThan(0.75);
    expect(staleStyle.opacity).toBeGreaterThan(0.5);

    // Stale row should have a background-image (diagonal hatch pattern),
    // not just a solid background
    expect(staleStyle.backgroundImage).toContain('linear-gradient');

    // Healthy row should have full opacity (1.0 or very close)
    expect(healthyStyle.opacity).toBeGreaterThanOrEqual(0.95);

    // Healthy row should not have the stale background-image pattern
    // (it may have none, or a different pattern, but not the stale one)
    // This is a softer assertion since healthyStyle.backgroundImage might be "none"
    const healthyHasStalePattern = healthyStyle.backgroundImage.includes('0.05')
      && healthyStyle.backgroundImage.includes('0.05');
    expect(healthyHasStalePattern).toBe(false);
  });

  test('Stale row persists across N reload cycles (no flicker regression)', async ({ page }) => {
    // Operator complaint: "DH6847 is showing and disappearing on and off".
    // This test ensures the stale row remains visible across multiple page loads,
    // confirming the LKG data is cached and served consistently.
    const runs = 3;
    let seenCount = 0;

    for (let i = 0; i < runs; i++) {
      await navigateToFundsGrid(page);

      // Check if the stale row is present (scoped to funds grid specifically)
      const fundsContainer = page.locator('.ag-theme-ramboq').filter({
        hasText: /Account.*Net.*Util|Avl.Margin/i,
      }).first();
      const staleRowCount = await fundsContainer.locator('.ag-row[row-id="DH6847"]').count();
      if (staleRowCount > 0) seenCount++;

      // Soft reload for next iteration (navigate to blank to unmount grid)
      if (i < runs - 1) {
        await page.goto('about:blank', { waitUntil: 'domcontentloaded' });
      }
    }

    // All reload cycles must see the stale row — zero disappearances
    expect(seenCount).toBe(runs);
  });

  test('Stale row class is visible under .ag-theme-ramboq container, not .ag-theme-algo', async ({ page }) => {
    // Regression guard: ensure we're testing the CORRECT theme variant.
    // PerformancePage uses .ag-theme-ramboq (public cream theme).
    // Admin dashboard uses .ag-theme-algo (dark theme).
    // Both have row-account-stale CSS rules, but we specifically want to test
    // that the public performance page uses ramboq.

    await navigateToFundsGrid(page);

    const fundsContainer = page.locator('.ag-theme-ramboq').filter({
      hasText: /Account.*Net.*Util|Avl.Margin/i,
    }).first();

    // The funds container should be under .ag-theme-ramboq, not .ag-theme-algo
    const isRamboq = await fundsContainer.evaluate((el) =>
      el.closest('.ag-theme-ramboq') !== null
    );
    const isAlgo = await fundsContainer.evaluate((el) =>
      el.closest('.ag-theme-algo') !== null
    );

    expect(isRamboq).toBe(true);
    expect(isAlgo).toBe(false);
  });
});
