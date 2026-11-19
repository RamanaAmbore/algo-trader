/**
 * swr_orders_grid_reload_survival.spec.js
 *
 * PLAN.md Part B5 (SWR extension), surface 1 of 3 — the Orders grid.
 *
 * Feature under test (B1): `ordersStore` (marketDataStores.svelte.js, key
 * `md.orders`) persists the merged broker+algo order book to Tier-2
 * localStorage, and OrderBook.svelte reads `ordersStore.value` instead of
 * its own local `orderRows`/`_loading` state. Before B1 a hard reload of
 * /orders painted "Loading…" until the first fetch resolved.
 *
 * Assertion shape (reload-survival, the strongest form):
 *   1. First load: both order routes are fulfilled with deterministic
 *      fixture rows, so the grid renders a known symbol and the store
 *      writes `rbq.cache.md.orders` to localStorage.
 *   2. Hard reload with BOTH order routes HELD (the request never resolves
 *      while the test is looking). The only thing that can paint the
 *      fixture symbol now is the Tier-2 cache.
 *   3. Assert the fixture symbol is visible while the held fetch is still
 *      pending, and that "Loading…" is not shown for the order book.
 *   4. Release the held routes so the spec leaves no hanging requests.
 *
 * Deterministic fixtures are used rather than live dev orders because the
 * dev account may hold no OPEN/today orders at any given time; a "rows
 * survive reload" check against an empty book would be vacuous.
 *
 * Run:
 *   npx playwright test e2e/swr_orders_grid_reload_survival.spec.js --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BROKER_ROUTE = /\/api\/orders\/(\?.*)?$/;
const ALGO_ROUTE = /\/api\/orders\/algo\/recent/;
const FIXTURE_SYMBOL = 'SPECTESTSYM1';

// Status OPEN is never dropped by the session-boundary filter, so the
// fixture row survives regardless of the wall-clock time the test runs.
const BROKER_BODY = JSON.stringify({
  rows: [{
    order_id: 'PWTEST00000000001',
    account: 'ZG0790',
    exchange: 'NFO',
    tradingsymbol: FIXTURE_SYMBOL,
    transaction_type: 'BUY',
    quantity: 50,
    pending_quantity: 50,
    filled_quantity: 0,
    price: 123.45,
    trigger_price: 0,
    average_price: 0,
    status: 'OPEN',
    order_type: 'LIMIT',
    product: 'NRML',
    order_timestamp: new Date().toISOString(),
  }],
});

test.describe('SWR reload survival — Orders grid (B1)', () => {
  test('grid paints cached order rows on hard reload before the delayed fetch lands', async ({ page }) => {
    await loginAsAdmin(page);

    let hold = false;
    /** @type {Array<() => Promise<void>>} */
    const pending = [];
    const fulfil = (route, body) => route.fulfill({
      status: 200, contentType: 'application/json', body,
    });
    const handle = (body) => (route) => {
      if (hold) {
        pending.push(() => fulfil(route, body));
        return;
      }
      return fulfil(route, body);
    };
    await page.route(BROKER_ROUTE, handle(BROKER_BODY));
    await page.route(ALGO_ROUTE, handle('[]'));

    // ── 1. First load: populate Tier-2 ──────────────────────────────
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    await expect(page.getByText(FIXTURE_SYMBOL).first()).toBeVisible({ timeout: 30_000 });
    await expect
      .poll(() => page.evaluate(() => !!localStorage.getItem('rbq.cache.md.orders')), { timeout: 10_000 })
      .toBe(true);

    // ── 2. Hard reload with the order routes held ───────────────────
    hold = true;
    await page.reload({ waitUntil: 'domcontentloaded' });

    // ── 3. Cached rows visible while the network response is pending ─
    await expect(page.getByText(FIXTURE_SYMBOL).first()).toBeVisible({ timeout: 15_000 });
    expect(pending.length, 'order fetch should still be held when cached rows paint').toBeGreaterThan(0);

    // The order book must not be stuck on its cold-start placeholder.
    // OrderBook renders "Loading…" only when ordersStore has no value yet.
    const orderBookLoading = page.locator('.log-debug', { hasText: 'Loading…' });
    await expect(orderBookLoading).toHaveCount(0);

    // ── 4. Release held requests ────────────────────────────────────
    hold = false;
    await Promise.all(pending.map((release) => release()));
  });
});
