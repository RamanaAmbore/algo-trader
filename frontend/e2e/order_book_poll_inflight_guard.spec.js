/**
 * order_book_poll_inflight_guard.spec.js
 *
 * Audit fix (2026-10-02): OrderBook.svelte's `_loadOrders`/`_loadGtts` ran
 * on every `visibleInterval` poll tick with no guard against a slow
 * response overlapping the next tick. Fixed by wrapping the poll-cadence
 * callers (onMount's immediate call + every interval tick, sharing the
 * SAME guarded instance — mirrors LogPanel.svelte's `_every()` pattern)
 * with `withGuard()` from `$lib/stores` (OrderBook.svelte onMount, ~line
 * 271). Manual triggers (Cancel/Reconcile reload, CardHeader's Refresh
 * button) deliberately still call the raw, unguarded functions — not
 * under test here.
 *
 * This spec intercepts `GET /api/orders/gtts/` (`fetchGtts()`) — the ONE
 * request only OrderBook's own `_loadGtts` ever makes anywhere in this
 * app (verified: no other caller of `fetchGtts()` exists). This matters
 * because `/api/orders/algo/recent` (`fetchAlgoOrdersRecent`, the OTHER
 * half of OrderBook's merged poll) is NOT exclusive to OrderBook — the
 * app-wide fill-watch backstop poller in `(algo)/+layout.svelte` polls
 * the identical endpoint independently for an unrelated purpose, so
 * counting THAT endpoint's requests would conflate two separate pollers
 * and produce false positives. `/orders/gtts/` has no such collision.
 * The response is held well past `pollMs` (default 5000ms, unset on this
 * page) to force poll ticks to land while a prior response is still
 * outstanding. Asserts the guard caps concurrent in-flight requests at 1
 * across several tick windows.
 *
 * Quality dimensions:
 * - SSOT: observes real network timing via page.route(), not a mocked
 *   internal flag — proves the guard end-to-end through the real
 *   visibleInterval/withGuard wiring, not just the withGuard unit itself
 *   (already covered by withGuard.test.js).
 * - Perf: this IS the perf/correctness invariant under test — no stacked
 *   concurrent broker calls on a slow network.
 * - Stale/regression: removing the guard (reverting to the bare
 *   `_loadOrders(); _loadGtts();` interval callback) reproduces >1
 *   concurrent in-flight requests — verified manually before landing
 *   this spec.
 * - Reusable: shares the login()/BASE pattern used by the sibling
 *   `order_book_freeze_on_broker_fetch_fail.spec.js`.
 * - UX: the invariant under test IS the UX guarantee — a slow network
 *   never causes a stampede of duplicate broker/algo-order fetches.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/order_book_poll_inflight_guard.spec.js --project=chromium-desktop
 */
import { test, expect } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
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

test('OrderBook poll never overlaps a slow response (in-flight guard)', async ({ page }) => {
  test.setTimeout(60_000);
  await login(page);

  // Delay comfortably past OrderBook's default pollMs (5000ms, unset on
  // this page) so at least two interval ticks land while the first
  // response is still outstanding — the exact overlap scenario the
  // guard must prevent.
  const DELAY_MS = 7_000;
  let inFlight = 0;
  let maxInFlight = 0;
  let totalCalls = 0;

  await page.route('**/api/orders/gtts/**', async (route) => {
    totalCalls += 1;
    inFlight += 1;
    maxInFlight = Math.max(maxInFlight, inFlight);
    try {
      const resp = await route.fetch();
      await new Promise((r) => setTimeout(r, DELAY_MS));
      await route.fulfill({ response: resp });
    } catch {
      await route.fulfill({ status: 200, body: JSON.stringify({ gtts: [] }) });
    } finally {
      inFlight -= 1;
    }
  });

  await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });

  // Observe across ~3 poll-tick windows (pollMs=5000 default): t=0 (mount),
  // t=5s, t=10s, t=15s. Guarded code fires a fresh request only once the
  // prior one (held DELAY_MS=7s) has resolved — roughly every 7-10s, never
  // overlapping. Unguarded code would fire a new request every 5s
  // regardless of the prior one still being outstanding.
  await page.waitForTimeout(16_000);

  await page.unroute('**/api/orders/gtts/**');

  expect(maxInFlight, 'at most one in-flight /orders/gtts/ request at any time').toBeLessThanOrEqual(1);
  // Defensive regression guard: unguarded polling over this window would
  // fire ~4 requests (t=0,5,10,15); guarded polling fires ~2-3 (one per
  // DELAY_MS=7s cycle). A regression that removes the guard would also
  // push this count up alongside maxInFlight.
  expect(totalCalls, 'guarded poll fires far fewer requests than the raw tick cadence').toBeLessThanOrEqual(3);
});
