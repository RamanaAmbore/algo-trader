/**
 * order_book_freeze_on_broker_fetch_fail.spec.js
 *
 * Operator report: "OrderBook showing cancelled orders as OPEN again".
 * Root cause: OrderBook.svelte's _loadOrders() merges the broker order
 * book (fetchOrders() — authoritative for CANCELLED/COMPLETE/REJECTED,
 * confirmed at the exchange) with the algo-tracked order book
 * (fetchAlgoOrdersRecent() — can lag a just-confirmed cancel/fill until
 * the postback/reconcile pass catches up). Pre-fix, a transient
 * fetchOrders() failure (Promise.allSettled 'rejected') silently fell
 * through to brokerRows=[], so the ENTIRE merged view came from the
 * algo book alone for that poll — a terminal order could flicker back
 * to OPEN purely because the broker FETCH failed, not because the
 * order itself reopened.
 *
 * Fix: OrderBook.svelte now freezes to the last-known-good merged view
 * whenever the broker fetch rejects, instead of recomputing from
 * partial (algo-only) data.
 *
 * This spec intercepts /api/orders/ to force one poll cycle to fail
 * after a real first load succeeded, and asserts the rendered rows are
 * unchanged (frozen) rather than collapsing or reverting.
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=http://localhost:5173 \
 *     npx playwright test e2e/order_book_freeze_on_broker_fetch_fail.spec.js \
 *     --project=chromium-desktop
 */
import { test, expect } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

let _cachedToken = null;
async function login(page) {
  if (!_cachedToken) {
    for (const u of ['rambo', 'ambore', 'admin']) {
      const r = await page.request.post(`${BASE}/api/auth/login`, {
        data: { username: u, password: _AUTH_PASS },
        timeout: 15_000,
      }).catch(() => null);
      if (r && r.ok()) { _cachedToken = (await r.json()).access_token; break; }
    }
    if (!_cachedToken) throw new Error('login failed');
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, _cachedToken);
}

test('OrderBook freezes to last-known-good rows when the broker fetch fails', async ({ page }) => {
  test.setTimeout(60_000);
  await login(page);
  await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);

  const cards = page.locator('.oc-book-grid .order-card');
  const before = await cards.count();
  if (before === 0) {
    test.skip(true, 'no order rows rendered — nothing to freeze/compare, skip');
    return;
  }
  const beforeIds = await cards.evaluateAll(
    (els) => els.map((el) => (el.getAttribute('data-status') || '') + '|' + (el.textContent?.slice(0, 60) || ''))
  );

  // Force the next broker order-book fetch to fail (simulates a
  // transient network blip / backend 5xx) while leaving the algo-order
  // fetch untouched, reproducing the exact partial-failure shape.
  await page.route('**/api/orders/', (route) => {
    if (route.request().method() === 'GET') {
      route.fulfill({ status: 500, body: 'simulated broker fetch failure' });
    } else {
      route.continue();
    }
  });

  // Wait past at least one poll cycle (default pollMs=3000).
  await page.waitForTimeout(4000);

  const afterCount = await cards.count();
  const afterIds = await cards.evaluateAll(
    (els) => els.map((el) => (el.getAttribute('data-status') || '') + '|' + (el.textContent?.slice(0, 60) || ''))
  );

  expect(afterCount, 'row count must stay frozen across a failed broker-fetch poll').toBe(before);
  expect(afterIds, 'row identities must stay frozen across a failed broker-fetch poll').toEqual(beforeIds);

  await page.unroute('**/api/orders/');
});
