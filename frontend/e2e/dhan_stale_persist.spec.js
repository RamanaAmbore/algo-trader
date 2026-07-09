/**
 * Dhan stale-account persistence — Playwright e2e spec.
 *
 * Operator complaint (2026-07-03): "dhan is showing and disappearing accounts
 * on and off". Root cause: DH6847 has circuit_breaker_enabled=True; when the
 * breaker opens, `_fetch_positions_local` / `_fetch_holdings_local` /
 * `_fetch_margins_local` short-circuit and return an empty DataFrame. The
 * empty frame gets concatenated away, so DH6847 rows silently vanish from
 * the payload. On the next successful poll the rows reappear — the flicker.
 *
 * Fix under test: backend now substitutes the LKG frame + surfaces
 * `account_stale=true` on each row + `stale_accounts=['DH6847']` on the
 * response. Frontend applies the `row-account-stale` CSS class (slate
 * desaturation + diagonal hatch) so the rows persist visibly across
 * breaker-open cycles.
 *
 * This spec:
 *  1. Mocks /api/positions to return a DH6847 row with account_stale=true
 *     and a ZG0790 row with account_stale=false.
 *  2. Verifies BOTH rows render (DH6847 is NOT dropped) — the operator's
 *     core complaint.
 *  3. Verifies the DH6847 row carries the row-account-stale CSS class
 *     (the visual staleness indicator).
 *  4. Verifies the ZG0790 row does NOT carry the stale class.
 *
 * Five quality dimensions:
 *  1. SSOT     — row-account-stale is defined once in app.css; getRowClass
 *                is the single decision point in MarketPulse.svelte.
 *  2. Perf     — no backend calls beyond mocks; renders in-viewport only.
 *  3. Stale    — asserts that the stale row DOES NOT vanish from the grid
 *                across N reload cycles (the flicker regression guard).
 *  4. Reuse    — same route.fulfill pattern as closed_hours_day_change spec.
 *  5. UX       — asserts CSS class name + row count both — the visual
 *                consistency the operator observed as missing.
 *
 * Run context: chromium-desktop (grid mounts require desktop viewport).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 30_000;

// Mocked market status — must be OPEN so MarketPulse doesn't route through
// the closed-hours snapshot path. The staleness we're testing is the
// broker breaker-open case, which only fires during live market hours.
const MARKET_OPEN = {
  nse_open: true,
  mcx_open: true,
  any_open: true,
  is_holiday: false,
};

// ZG0790 row — healthy account, fresh broker fetch. Uses standard PositionRow
// fields; `account_stale` defaults to false so we omit it explicitly.
const HEALTHY_POSITION = {
  account: 'ZG0790',
  tradingsymbol: 'NIFTY26JUL22000CE',
  exchange: 'NFO',
  product: 'NRML',
  quantity: 50,
  average_price: 100.0,
  close_price: 102.0,
  last_price: 105.0,
  pnl: 250.0,
  pnl_percentage: 5.0,
  day_change_val: 150.0,
  day_change_percentage: 1.47,
  account_stale: false,
};

// DH6847 row — breaker-open account, served from LKG cache.
// account_stale=true is the load-bearing field.
const STALE_POSITION = {
  account: 'DH6847',
  tradingsymbol: 'BANKNIFTY26JUL48000PE',
  exchange: 'NFO',
  product: 'NRML',
  quantity: -25,
  average_price: 200.0,
  close_price: 195.0,
  last_price: 190.0,
  pnl: 250.0,
  pnl_percentage: 5.0,
  day_change_val: 125.0,
  day_change_percentage: 0.63,
  account_stale: true,
};

/**
 * Install route mocks: market status open + positions with mixed
 * fresh/stale rows.
 *
 * @param {import('@playwright/test').Page} page
 */
async function installStalePositionsMocks(page) {
  await page.route('**/api/market/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(MARKET_OPEN),
    })
  );

  await page.route('**/api/positions*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        rows: [HEALTHY_POSITION, STALE_POSITION],
        summary: [
          { account: 'ZG0790', pnl: 250.0, day_change_val: 150.0,
            day_change_percentage: 1.47, day_prev_val: 10200.0 },
          { account: 'DH6847', pnl: 250.0, day_change_val: 125.0,
            day_change_percentage: 0.63, day_prev_val: 19875.0 },
          { account: 'TOTAL',  pnl: 500.0, day_change_val: 275.0,
            day_change_percentage: 0.91, day_prev_val: 30075.0 },
        ],
        refreshed_at: 'Fri 03 Jul 11:15 IST',
        stale_accounts: ['DH6847'],   // the response-level flag
      }),
    })
  );

  // Empty holdings + funds so we focus on positions.
  await page.route('**/api/holdings*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        rows: [], summary: [], refreshed_at: 'Fri 03 Jul 11:15 IST',
        stale_accounts: [],
      }),
    })
  );

  await page.route('**/api/funds*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        rows: [], refreshed_at: 'Fri 03 Jul 11:15 IST',
        stale_accounts: [],
      }),
    })
  );
}

async function waitForPulseGrid(page) {
  await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
  await page.locator('.ag-center-cols-container .ag-row').first()
    .waitFor({ state: 'attached', timeout: TIMEOUT });
}

test.describe('Dhan stale-account persistence — desktop', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await installStalePositionsMocks(page);
  });

  test('DH6847 row renders even when marked account_stale=true', async ({ page }) => {
    await waitForPulseGrid(page);

    // Both accounts must be visible in the positions grid. Before the fix,
    // DH6847 was silently dropped from the payload on breaker-open cycles.
    const accounts = await page.evaluate(() => {
      const rows = document.querySelectorAll(
        '.ag-center-cols-container .ag-row[row-id]'
      );
      const found = new Set();
      for (const row of rows) {
        // The row-id embeds the account; also look at any cell text.
        const rowId = row.getAttribute('row-id') || '';
        if (rowId.includes('ZG0790')) found.add('ZG0790');
        if (rowId.includes('DH6847')) found.add('DH6847');
        // Fallback — walk cell text.
        const cells = row.querySelectorAll('.ag-cell');
        for (const cell of cells) {
          const t = (cell.textContent || '').trim();
          if (t.includes('ZG0790')) found.add('ZG0790');
          if (t.includes('DH6847')) found.add('DH6847');
        }
      }
      return Array.from(found);
    });

    expect(accounts).toContain('ZG0790');
    expect(accounts).toContain('DH6847');
  });

  test('DH6847 row carries the row-account-stale CSS class', async ({ page }) => {
    await waitForPulseGrid(page);

    // Find the DH6847 row and read its class list. The staleness class
    // is the direct visual indicator the operator sees.
    const hasStaleClass = await page.evaluate(() => {
      const rows = document.querySelectorAll(
        '.ag-center-cols-container .ag-row[row-id]'
      );
      for (const row of rows) {
        const rowText = (row.textContent || '');
        if (rowText.includes('DH6847') || rowText.includes('BANKNIFTY')) {
          return row.classList.contains('row-account-stale');
        }
      }
      return false;
    });
    expect(hasStaleClass).toBe(true);
  });

  test('ZG0790 row does NOT carry the row-account-stale class', async ({ page }) => {
    await waitForPulseGrid(page);

    // Healthy account must NOT be tinted stale — that would be a false
    // positive (operator would think their live account is offline).
    const zgHasStaleClass = await page.evaluate(() => {
      const rows = document.querySelectorAll(
        '.ag-center-cols-container .ag-row[row-id]'
      );
      for (const row of rows) {
        const rowText = (row.textContent || '');
        if (rowText.includes('ZG0790') || rowText.includes('NIFTY26JUL22000CE')) {
          return row.classList.contains('row-account-stale');
        }
      }
      return null;
    });
    expect(zgHasStaleClass).toBe(false);
  });

  test('DH6847 row persists across N reload cycles (no flicker)', async ({ page }) => {
    // The operator's core complaint: "dhan is showing and disappearing on
    // and off". Verify that a DH6847 row with account_stale=true renders
    // consistently across multiple grid re-mounts. Regression guard.
    const runs = 5;
    let seenCount = 0;
    for (let i = 0; i < runs; i++) {
      await waitForPulseGrid(page);
      const present = await page.evaluate(() => {
        const rows = document.querySelectorAll(
          '.ag-center-cols-container .ag-row[row-id]'
        );
        for (const row of rows) {
          if ((row.textContent || '').includes('DH6847') ||
              (row.textContent || '').includes('BANKNIFTY26JUL48000PE')) {
            return true;
          }
        }
        return false;
      });
      if (present) seenCount++;
      // Force a soft re-navigate so the grid remounts on the next iteration.
      if (i < runs - 1) {
        await page.goto('about:blank');
      }
    }
    // All N cycles must see DH6847 — zero disappearances.
    expect(seenCount).toBe(runs);
  });
});
