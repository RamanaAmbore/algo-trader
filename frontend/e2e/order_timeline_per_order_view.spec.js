/**
 * order_timeline_per_order_view.spec.js
 *
 * Covers the per-order timeline view added to OrderBook.svelte: clicking
 * an OrderCard's body now opens a LOCAL OrderTimelineDrawer instance
 * scoped to that one order (`GET /api/orders/{id}/events`) — a
 * different concept from (algo)/+layout.svelte's navbar chase-chip
 * drawer, which shows an AGGREGATE feed across every open chase order
 * via `GET /api/orders/events/recent?status=open`
 * (order_timeline_drawer_field_fix.spec.js covers that surface).
 *
 * Also covers the additive linked-orders chip strip on
 * OrderTimelineDrawer.svelte (parent/child/basket), gated entirely on
 * the new optional `linkedOrders` prop — the navbar drawer never passes
 * it, so this is scoped to OrderBook.svelte's new wiring only.
 *
 * Four scenarios:
 *   1. Clicking a card opens the drawer with THAT order's own events —
 *      not the aggregate chase feed (which is seeded with a DIFFERENT
 *      order_id/symbol so a wrong-feed bug would be visible).
 *   2. Clicking Modify / Cancel / Reconcile does NOT also open the
 *      drawer (confirms each button's existing e.stopPropagation()
 *      still blocks the card's own onclick).
 *   3. A row with parent_order_id + child_order_ids renders the chip
 *      strip; clicking a chip re-fetches and shows THAT order's events.
 *   4. A row with none of parent_order_id/child_order_ids/basket_tag
 *      set renders no chip strip at all.
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

/** A plain OPEN broker row — `_rowOrigin: 'broker'` is stamped by
 *  OrderBook's own `_loadOrders()` merge step for every row returned
 *  from `fetchOrders()` (not `fetchAlgoOrdersRecent()`), which is what
 *  gates the Modify/Cancel buttons (`_isOpenBroker`) — so this row must
 *  come back from the `/api/orders/` (plain) response, never
 *  `/api/orders/algo/recent`. */
function brokerRow(overrides = {}) {
  return {
    order_id: '201', tradingsymbol: 'NIFTY26JUN25000CE', exchange: 'NFO',
    transaction_type: 'BUY', quantity: 50, status: 'OPEN',
    order_timestamp: '2026-01-01 09:00:00', price: 100, account: 'T1',
    variety: 'regular', product: 'MIS',
    ...overrides,
  };
}

/** Mocks every /api/orders/** endpoint OrderBook.svelte + the (algo)
 *  layout's own aggregate chase feed touch when /orders is loaded.
 *    - `rows`           → GET /api/orders/ (the clicked row(s))
 *    - `aggregateEvents`→ GET /api/orders/events/recent (DELIBERATELY a
 *                         different order_id so a bug that read this
 *                         feed instead of the per-order one is visible)
 *    - `eventsById`     → GET /api/orders/{id}/events, keyed by id
 *  Cancel (DELETE) / Reconcile (POST .../reconcile) are fulfilled with
 *  a harmless response so the "doesn't open the drawer" assertion
 *  isn't flaky on whatever a real backend would do with a fixture-only
 *  order id. */
async function mockOrderBookPage(page, { rows, aggregateEvents = [], eventsById = {} }) {
  await page.route('**/api/orders/**', async (route) => {
    const req = route.request();
    const url = req.url();
    const method = req.method();

    if (method === 'GET') {
      const perOrderMatch = url.match(/\/orders\/(\d+)\/events(?:\?|$)/);
      if (perOrderMatch) {
        const id = perOrderMatch[1];
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify(eventsById[id] ?? eventsById[Number(id)] ?? []),
        });
        return;
      }
      if (url.includes('/orders/events/recent')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(aggregateEvents) });
        return;
      }
      if (url.includes('/orders/algo/recent')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
        return;
      }
      if (url.includes('/orders/gtts')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ gtts: [], count: 0 }) });
        return;
      }
      if (/\/api\/orders\/?(\?.*)?$/.test(url)) {
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({ rows, refreshed_at: new Date().toISOString() }),
        });
        return;
      }
      await route.continue();
      return;
    }
    if (method === 'DELETE') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) });
      return;
    }
    if (method === 'POST' && url.includes('/reconcile')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ updated: false }) });
      return;
    }
    await route.continue();
  });
}

test.describe('OrderBook — per-order timeline drawer', () => {
  test.setTimeout(60_000);

  test('clicking a card opens the drawer with that order\'s own events, not the aggregate chase feed', async ({ page }) => {
    const row = brokerRow();
    await loginAsAdmin(page);
    await mockOrderBookPage(page, {
      rows: [row],
      // Aggregate feed deliberately carries a DIFFERENT order — if the
      // drawer ever read this instead of the per-order fetch, the
      // assertions below on symbol/price would fail.
      aggregateEvents: [
        { id: 90, order_id: 999, ts: '2026-01-01T00:00:00Z', kind: 'placed', message: 'wrong feed', payload_json: null },
      ],
      eventsById: {
        201: [
          { id: 1, order_id: 201, ts: '2026-01-01T09:00:00Z', kind: 'placed', message: 'x', payload_json: null },
          { id: 2, order_id: 201, ts: '2026-01-01T09:05:00Z', kind: 'chase_modify', message: 'x', payload_json: JSON.stringify({ price: 111 }) },
        ],
      },
    });

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');

    const card = page.locator('.order-card').filter({ hasText: /NIFTY.*25000.*CE/ });
    await expect(card).toBeVisible({ timeout: 15_000 });
    await card.click();

    const drawer = page.locator('.otd-drawer');
    await expect(drawer).toBeVisible();
    // Real per-order data: the clicked order's own symbol + price.
    await expect(drawer.locator('.otd-symbol')).toHaveText(/NIFTY.*25000.*CE/);
    await expect(drawer.locator('.otd-ev-price', { hasText: '₹111.00' })).toBeVisible();
    // Never the aggregate feed's order_id 999 / "wrong feed" row.
    await expect(drawer).not.toContainText('Order #999');
    await expect(drawer.locator('.otd-section')).toHaveCount(1);
    // No linked-orders chip strip — this row has none of
    // parent_order_id/child_order_ids/basket_tag set.
    await expect(page.locator('.otd-linked')).toHaveCount(0);
  });

  test('clicking Modify / Cancel / Reconcile does NOT also open the timeline drawer', async ({ page }) => {
    const row = brokerRow();
    await loginAsAdmin(page);
    await mockOrderBookPage(page, { rows: [row], eventsById: { 201: [] } });

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');

    const card = page.locator('.order-card').filter({ hasText: /NIFTY.*25000.*CE/ });
    await expect(card).toBeVisible({ timeout: 15_000 });

    const drawer = page.locator('.otd-drawer');

    await card.locator('.lp-oc-cancel').click();
    await page.waitForTimeout(300);
    await expect(drawer).not.toBeVisible();

    // Re-query the card — a cancel attempt can trigger a reload of
    // orderRows; same mocked row still comes back from the fixture.
    const cardAfterCancel = page.locator('.order-card').filter({ hasText: /NIFTY.*25000.*CE/ });
    await cardAfterCancel.locator('.lp-oc-reconcile').click();
    await page.waitForTimeout(300);
    await expect(drawer).not.toBeVisible();

    const cardAfterReconcile = page.locator('.order-card').filter({ hasText: /NIFTY.*25000.*CE/ });
    await cardAfterReconcile.locator('.lp-oc-modify').click();
    await page.waitForTimeout(300);
    await expect(drawer).not.toBeVisible();
  });

  test('parent/child linked-orders chip strip appears and switches the drawer to the clicked linked order', async ({ page }) => {
    const row = brokerRow({
      order_id: '301', tradingsymbol: 'BANKNIFTY26JUN50000PE', transaction_type: 'SELL',
      quantity: 25, order_timestamp: '2026-01-01 09:10:00',
      parent_order_id: 300, child_order_ids: [302],
    });
    await loginAsAdmin(page);
    await mockOrderBookPage(page, {
      rows: [row],
      eventsById: {
        301: [
          { id: 1, order_id: 301, ts: '2026-01-01T09:10:00Z', kind: 'placed', message: 'x', payload_json: null },
          { id: 2, order_id: 301, ts: '2026-01-01T09:11:00Z', kind: 'chase_modify', message: 'x', payload_json: JSON.stringify({ price: 211 }) },
        ],
        300: [
          { id: 3, order_id: 300, ts: '2026-01-01T08:00:00Z', kind: 'placed', message: 'y', payload_json: null },
          { id: 4, order_id: 300, ts: '2026-01-01T08:05:00Z', kind: 'fill', message: 'y', payload_json: JSON.stringify({ fill_price: 222.5 }) },
        ],
      },
    });

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');

    const card = page.locator('.order-card').filter({ hasText: /BANKNIFTY.*50000.*PE/ });
    await expect(card).toBeVisible({ timeout: 15_000 });
    await card.click();

    const drawer = page.locator('.otd-drawer');
    await expect(drawer).toBeVisible();
    await expect(drawer.locator('.otd-ev-price', { hasText: '₹211.00' })).toBeVisible();

    const linked = page.locator('.otd-linked');
    await expect(linked).toBeVisible();
    const parentChip = linked.locator('.otd-chip-link', { hasText: 'Parent: #300' });
    const childChip  = linked.locator('.otd-chip-link', { hasText: 'Child: #302' });
    await expect(parentChip).toBeVisible();
    await expect(childChip).toBeVisible();

    await parentChip.click();

    // Switched to order 300's own events — new price, terminal (fill).
    await expect(drawer.locator('.otd-ev-price', { hasText: '₹222.50' })).toBeVisible();
    await expect(drawer.locator('.otd-section.otd-section-terminal')).toHaveCount(1);
    // Order 300 isn't one of the loaded orderRows, so it has no resolved
    // symbol context — falls back to "Order #300" rather than a stale
    // or wrong symbol.
    await expect(drawer).toContainText('Order #300');
  });

  test('a row with no parent/child/basket fields renders no linked-orders chip strip', async ({ page }) => {
    const row = brokerRow();
    await loginAsAdmin(page);
    await mockOrderBookPage(page, { rows: [row], eventsById: { 201: [] } });

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');

    const card = page.locator('.order-card').filter({ hasText: /NIFTY.*25000.*CE/ });
    await expect(card).toBeVisible({ timeout: 15_000 });
    await card.click();

    await expect(page.locator('.otd-drawer')).toBeVisible();
    await expect(page.locator('.otd-linked')).toHaveCount(0);
  });
});
