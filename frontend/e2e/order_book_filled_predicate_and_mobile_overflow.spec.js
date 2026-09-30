/**
 * order_book_filled_predicate_and_mobile_overflow.spec.js
 *
 * Covers two OrderBook.svelte fixes:
 *
 *  1. `_STATUS_PREDICATES.complete` broadened to match EITHER the broker
 *     (Kite) vocabulary token 'COMPLETE' OR the AlgoOrder vocabulary
 *     token 'FILLED' (ALGO_ORDER_FINAL_STATUSES in backend/api/models.py).
 *     An AlgoOrder row carrying `status: 'FILLED'` (e.g. a paper/sim/
 *     replay/shadow fill, or any algo-tracked live order before the
 *     broker's own COMPLETE status lands) must now count under the
 *     "Filled" status chip and be visible in the grid when that chip is
 *     active — unblocking the tmpl:#N chip / Re-attach button gated on
 *     that row being reachable in the currently-active status-chip view.
 *
 *  2. `.ob-status-bar`'s 5 status chips (Chase/Open/Filled/Rejected/
 *     Cancelled) fit within a 320-375px phone viewport without forcing
 *     horizontal overflow on the page — `grid-template-columns:
 *     repeat(5, minmax(0, 1fr))` (was a plain `1fr`, which cannot shrink
 *     below the "CANCELLED" label's content width) plus truncation on
 *     `.ob-sc-l`.
 *
 * Mocks GET /api/orders/ (broker book) and GET /api/orders/algo/recent
 * (algo-tracked book) — same pattern as
 * order_book_chase_chip_and_session_reset.spec.js.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/order_book_filled_predicate_and_mobile_overflow.spec.js --project=chromium-desktop
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
// row inside today's 08:00 IST session boundary so the session filter
// (unrelated to this spec) never drops a row.
const _NOW_ISO = '2026-09-30T04:30:00.000Z';

const _BROKER_ROWS = [
  { order_id: 'B2001', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-BROKERFILL',
    transaction_type: 'BUY', quantity: 50, pending_quantity: 0, filled_quantity: 50,
    price: 100, trigger_price: 0, average_price: 101, status: 'COMPLETE',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-30 09:00:00' },
];

// Algo-only FILLED row — carries a template_id, never gets a broker
// COMPLETE status (paper/sim/replay/shadow fill).
const _ALGO_ROWS = [
  { id: 101, account: 'T1', symbol: 'RBQ-ALGOFILLED', exchange: 'NFO', transaction_type: 'BUY',
    quantity: 50, initial_price: 100, current_limit: 100, fill_price: 100.5,
    attempts: 0, status: 'FILLED', engine: 'paper', mode: 'paper', detail: null,
    template_id: 7, attached_gtts_json: '{"legs":[]}',
    created_at: '2026-09-30T04:10:00' },
];

async function mockOrdersEndpoints(page) {
  await page.route('**/api/orders/**', async (route) => {
    const req = route.request();
    if (req.method() !== 'GET') { await route.continue(); return; }
    const url = req.url();
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
        body: JSON.stringify({ rows: _BROKER_ROWS, refreshed_at: new Date().toISOString() }),
      });
      return;
    }
    await route.continue();
  });
}

test.describe('OrderBook — FILLED predicate + mobile status-bar overflow', () => {
  test.setTimeout(60_000);

  test('algo-only FILLED row counts under the "Filled" chip and renders when that chip is active', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockOrdersEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    // "Filled" count includes BOTH the broker COMPLETE row and the
    // algo-only FILLED row — the fix under test.
    const filledChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Filled$/ }) });
    await expect(filledChip.locator('.ob-sc-n')).toHaveText('2');

    await filledChip.click();
    await expect(page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-ALGOFILLED' })).toHaveCount(1);
    await expect(page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-BROKERFILL' })).toHaveCount(1);
  });

  for (const vw of [320, 375]) {
    test(`status-bar 5 chips fit without horizontal page overflow at ${vw}px width`, async ({ page }) => {
      await page.setViewportSize({ width: vw, height: 720 });
      await authOnce(page);
      await page.clock.setFixedTime(new Date(_NOW_ISO));
      await mockOrdersEndpoints(page);

      await page.goto('/orders');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
      await page.waitForTimeout(600);

      const chips = page.locator('.ob-status-bar .ob-sc');
      await expect(chips).toHaveCount(5);

      // The document must not scroll horizontally past the viewport —
      // a plain `1fr` track previously forced the grid wider than the
      // 320-375px card once the 5th (Chase) chip was added.
      const overflowX = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth
      );
      expect(overflowX).toBeLessThanOrEqual(1);

      // Each chip itself must not exceed its allotted grid track width —
      // confirms the chips shrank to fit rather than merely being clipped
      // by an ancestor's overflow:hidden.
      const barBox = await page.locator('.ob-status-bar').boundingBox();
      for (let i = 0; i < 5; i++) {
        const box = await chips.nth(i).boundingBox();
        expect(box.x + box.width).toBeLessThanOrEqual(barBox.x + barBox.width + 1);
      }
    });
  }
});
