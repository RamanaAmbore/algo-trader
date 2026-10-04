/**
 * order_book_broker_order_id_dedup.spec.js
 *
 * Sprint 2a (docs/proposals/SPRINT2_LAYER_INTEGRATION.md §1 finding 2, §2,
 * §4.1) — last remaining item. `AlgoOrderInfo` now surfaces real
 * `broker_order_id` / `source` / `agent_id` fields (backend commit landed
 * ahead of this fix). Three linked bugs, fixed together:
 *
 *  1. OrderBook.svelte's dedup compared a broker row's `order_id` against
 *     `algoRow.order_id || algoRow.id` — `AlgoOrderInfo` never carried
 *     `order_id`, so the comparison silently fell through to the algo
 *     row's own internal DB `id`, which can never equal a broker
 *     `order_id`. The dedup never matched: a live, algo-tracked order
 *     rendered TWICE (once as a bare broker row, once as an algo row).
 *     Fix: dedup on `broker_order_id` (the real shared identity key).
 *
 *  2. `_isOpenBroker` (gates the Modify/Cancel buttons) used `!o?.mode`
 *     as a proxy for "is this a bare broker row" — it happened to work
 *     only because broker rows carried no `mode` field, not because it
 *     was an explicit check. Fix: an explicit `_rowOrigin` flag stamped
 *     at merge time, read instead of the field-presence heuristic.
 *
 *  3. ChaseCard's own dedup (`chaseOrderIds`, built from
 *     `c.broker_order_id`) was dead for the identical reason — the field
 *     didn't exist on the API response. Now that it's real, an order
 *     already shown as an active chase row must NOT also appear in the
 *     orders page's "Pending Orders" list.
 *
 * This spec mocks GET /api/orders/, GET /api/orders/algo/recent,
 * GET /api/orders/gtts, and GET /api/orders/chases/active on the /orders
 * page (which mounts both ChaseCard and OrderBook) — same mocking
 * pattern as orderbook_gtt_chip.spec.js / order_book_filled_predicate_and_mobile_overflow.spec.js.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/order_book_broker_order_id_dedup.spec.js --project=chromium-desktop
 */
import { test, expect } from '@playwright/test';

const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
let _cachedAuth = null;

async function authOnce(page) {
  if (!_cachedAuth) {
    const envToken = process.env.PLAYWRIGHT_AUTH_TOKEN;
    let tok = envToken || null;
    if (!tok) {
      for (const delay of [0, 20000, 65000]) {
        if (delay) await new Promise((r) => setTimeout(r, delay));
        const resp = await page.request.post('/api/auth/login', {
          data: { username: _AUTH_USER, password: _AUTH_PASS },
        });
        if (resp.ok()) { tok = (await resp.json()).access_token; break; }
        if (resp.status() !== 429) throw new Error(`authOnce: /api/auth/login ${resp.status()}`);
      }
    }
    if (!tok) throw new Error('authOnce: login rate-limited');
    _cachedAuth = { token: tok, user_id: _AUTH_USER };
  }
  const { token, user_id } = _cachedAuth;
  await page.goto('/');
  await page.evaluate(({ tok, usr }) => {
    sessionStorage.setItem('ramboq_token', tok);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: usr, username: usr, role: 'admin', display_name: usr,
    }));
  }, { tok: token, usr: user_id });
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${token}` });
}

// Fixed "now" = 2026-09-30 10:00 IST = 2026-09-30T04:30:00Z — keeps every
// row inside today's 08:00 IST session boundary (unrelated session filter).
const _NOW_ISO = '2026-09-30T04:30:00.000Z';

// A single live order that exists BOTH as a broker row (fetchOrders()) and
// as an algo-tracked row (fetchAlgoOrdersRecent()) — the exact duplicate
// shape the dedup fix targets. The algo row carries `broker_order_id`
// equal to the broker row's `order_id`, matching what the backend now
// actually sends.
const _BROKER_ROWS = [
  { order_id: 'B5001', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-DEDUPE1',
    transaction_type: 'BUY', quantity: 50, pending_quantity: 50, filled_quantity: 0,
    price: 100, trigger_price: 0, average_price: 0, status: 'OPEN',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-30 09:05:00' },
];
const _ALGO_ROWS = [
  { id: 777, account: 'T1', symbol: 'RBQ-DEDUPE1', exchange: 'NFO', transaction_type: 'BUY',
    quantity: 50, initial_price: 100, current_limit: 100, fill_price: null,
    attempts: 0, status: 'OPEN', engine: 'live', mode: 'live', detail: null,
    created_at: '2026-09-30T04:05:00',
    broker_order_id: 'B5001', source: 'ticket', agent_id: null },
];

// A SECOND, independent order — managed by the chase engine. Appears in
// /chases/active (ChaseCard's own feed) AND would otherwise also appear
// in the orders page's plain broker order list if ChaseCard's dedup
// (chaseOrderIds, built from broker_order_id) didn't exclude it from
// Pending Orders.
const _CHASE_BROKER_ROW = {
  order_id: 'B6002', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-CHASEDUP',
  transaction_type: 'SELL', quantity: 25, pending_quantity: 25, filled_quantity: 0,
  price: 200, trigger_price: 0, average_price: 0, status: 'OPEN',
  order_type: 'LIMIT', product: 'MIS', variety: 'regular',
  order_timestamp: '2026-09-30 09:10:00',
};
const _CHASE_ROW = {
  id: 888, account: 'T1', symbol: 'RBQ-CHASEDUP', exchange: 'NFO', transaction_type: 'SELL',
  quantity: 25, initial_price: 200, current_limit: 200, fill_price: null,
  attempts: 1, status: 'OPEN', engine: 'live', mode: 'live', detail: null,
  created_at: '2026-09-30T04:08:00',
  broker_order_id: 'B6002', source: 'ticket', agent_id: null,
};

async function mockOrdersEndpoints(page) {
  await page.route('**/api/orders/**', async (route) => {
    const req = route.request();
    if (req.method() !== 'GET') { await route.continue(); return; }
    const url = req.url();
    if (url.includes('/orders/gtts')) {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ gtts: [], count: 0 }),
      });
      return;
    }
    if (url.includes('/orders/chases/active')) {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify([_CHASE_ROW]),
      });
      return;
    }
    if (url.includes('/orders/algo/recent')) {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify(_ALGO_ROWS),
      });
      return;
    }
    if (/\/api\/orders\/?(\?.*)?$/.test(url)) {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          rows: [..._BROKER_ROWS, _CHASE_BROKER_ROW],
          refreshed_at: new Date().toISOString(),
        }),
      });
      return;
    }
    await route.continue();
  });
}

test.describe('OrderBook / ChaseCard — broker_order_id dedup (Sprint 2a)', () => {
  test.setTimeout(60_000);

  test('a live order with both a broker row and an algo row renders exactly once in OrderBook', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockOrdersEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    // Default chip resolution lands on 'open' (no chase attempts on this
    // row, nothing filled/rejected) — the duplicate-prone row is visible
    // without an explicit click.
    const dedupedCard = page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-DEDUPE1' });
    await expect(dedupedCard).toHaveCount(1);
  });

  test('Modify/Cancel buttons render on the deduped broker-origin row, not suppressed by the new _rowOrigin flag', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockOrdersEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    const dedupedCard = page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-DEDUPE1' });
    await expect(dedupedCard).toHaveCount(1);
    await expect(dedupedCard.locator('.lp-oc-modify')).toHaveCount(1);
    await expect(dedupedCard.locator('.lp-oc-cancel')).toHaveCount(1);
  });

  test("ChaseCard's broker_order_id dedup excludes an active chase from the Pending Orders list", async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockOrdersEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.cc-grid', { timeout: 15_000 });
    await page.waitForTimeout(800);

    // The order renders as an active chase row (symbol carried in the
    // cc-col-sym span's title attribute, unmassaged by formatSymbol).
    const chaseRows = page.locator('.cc-grid .cc-row:not(.cc-row-h) .cc-col-sym[title="RBQ-CHASEDUP"]');
    await expect(chaseRows).toHaveCount(1);

    // It must NOT also appear as a "Pending Orders" row — that would mean
    // chaseOrderIds (built from c.broker_order_id) failed to exclude it,
    // i.e. the dedup is still dead.
    const pendingRows = page.locator('.cc-pending-row .cc-col-sym[title="RBQ-CHASEDUP"]');
    await expect(pendingRows).toHaveCount(0);
  });
});
