/**
 * swr_logpanel_orders_tab_reload_survival.spec.js
 *
 * PLAN.md Part B5 (SWR extension), surface 2 of 3 — one LogPanel tab.
 *
 * Feature under test (B2): LogPanel.svelte hydrates its Orders-tab state
 * (`orderRows` / `orderEvents` / `gttRows`) from Tier-2 localStorage at
 * component construction (`cachedRead('log.order.<mode>')`) and persists
 * each successful merge back via `cachedWrite`. Before B2 those $state
 * arrays started empty on every mount, so a hard reload of the Activity
 * surface showed "No orders today." until the first poll landed.
 *
 * Surface: /activity?tab=order (the standalone Activity page renders
 * ActivityLogSurface -> LogPanel with the Orders tab selected). The tab id
 * is `order` (singular) per ACTIVITY_TABS in activityStore.svelte.js.
 *
 * Assertion shape (same as the Orders-grid spec):
 *   1. First load: both order routes fulfilled with deterministic fixture
 *      rows; the Orders tab renders the fixture symbol and writes
 *      `rbq.cache.log.order.none` to localStorage.
 *   2. Hard reload with the order routes HELD.
 *   3. Assert the fixture symbol is visible while the fetch is still
 *      pending, and the tab does NOT show its cold-start empty state.
 *   4. Release the held routes.
 *
 * Fixtures are used because the dev account may have no order rows today.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BROKER_ROUTE = /\/api\/orders\/(\?.*)?$/;
const ALGO_ROUTE = /\/api\/orders\/algo\/recent/;
const EVENTS_ROUTE = /\/api\/orders\/events\/recent/;
const FIXTURE_SYMBOL = 'SPECTESTSYM2';

// OPEN is never dropped by the session-boundary filter.
const BROKER_BODY = JSON.stringify({
  rows: [{
    order_id: 'PWTEST00000000002',
    account: 'ZG0790',
    exchange: 'NFO',
    tradingsymbol: FIXTURE_SYMBOL,
    transaction_type: 'SELL',
    quantity: 75,
    pending_quantity: 75,
    filled_quantity: 0,
    price: 88.5,
    trigger_price: 0,
    average_price: 0,
    status: 'OPEN',
    order_type: 'LIMIT',
    product: 'NRML',
    order_timestamp: new Date().toISOString(),
    // LogPanel gates order rows on executionMode when gateByMode is active
    // (same convention as log_panel_order_scroll.spec.js) — the fixture
    // rows must carry the mode the mocked execution poll reports below.
    mode: 'paper',
  }],
});

test.describe('SWR reload survival — LogPanel Orders tab (B2)', () => {
  test('Orders tab paints cached rows on hard reload before the delayed fetch lands', async ({ page }) => {
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
    // Boot mode defaults to 'idle' until the (algo) layout's execution-mode
    // poll lands; pin it to 'paper' so the paper-mode fixture rows render.
    await page.route('**/api/admin/execution/mode', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ mode: 'paper', allowed_modes: ['paper', 'live', 'shadow', 'sim', 'replay'], branch: 'dev' }),
    }));
    await page.route(BROKER_ROUTE, handle(BROKER_BODY));
    await page.route(ALGO_ROUTE, handle('[]'));
    await page.route(EVENTS_ROUTE, handle('[]'));

    // ── 1. First load: populate the LogPanel order-tab Tier-2 entry ──
    await page.goto('/activity?tab=order', { waitUntil: 'domcontentloaded' });
    await expect(page.getByText(FIXTURE_SYMBOL).first()).toBeVisible({ timeout: 30_000 });
    await expect
      .poll(() => page.evaluate(() => {
        for (let i = 0; i < localStorage.length; i++) {
          const k = localStorage.key(i);
          if (k && k.startsWith('rbq.cache.log.order.')) return true;
        }
        return false;
      }), { timeout: 10_000 })
      .toBe(true);

    // ── 2. Hard reload with the order routes held ───────────────────
    hold = true;
    await page.reload({ waitUntil: 'domcontentloaded' });

    // ── 3. Cached rows visible while the network response is pending ─
    await expect(page.getByText(FIXTURE_SYMBOL).first()).toBeVisible({ timeout: 15_000 });
    expect(pending.length, 'order fetch should still be held when cached rows paint').toBeGreaterThan(0);
    // Cold-start empty state the pre-B2 component painted until its first
    // fetch resolved. Must not be shown while cached rows are on screen.
    await expect(page.getByText('No orders today.')).toHaveCount(0);

    // ── 4. Release held requests ────────────────────────────────────
    hold = false;
    await Promise.all(pending.map((release) => release()));
  });
});
