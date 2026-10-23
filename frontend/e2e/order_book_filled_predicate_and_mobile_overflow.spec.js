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
 *  2. `.ob-status-bar`'s 6 status chips (Held-Attn/Chase/Open/Filled/
 *     Rejected-Cancelled/GTT) stay fully READABLE at a 320-375px phone
 *     viewport without forcing horizontal overflow on the PAGE.
 *     2026-10-23 fix (amends the original `repeat(6, minmax(0, 1fr))` +
 *     `.ob-sc-l` ellipsis-truncation design documented here previously):
 *     that grid squeezed "Rejected/Cancelled" (the longest label) into
 *     unreadable fragments — a silent-clip violation of this app's
 *     overflow-not-clip convention. Now a horizontally-scrolling flex row
 *     (`overflow-x: auto`, children `flex-shrink: 0`-equivalent via
 *     `flex: 1 0 auto`) — every chip keeps its full label; the ROW
 *     scrolls on narrow viewports instead of the page, and instead of
 *     clipping text.
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
    // Deterministic empty GTT set — checked before the bare-/orders/
    // regex below so it never falls through to route.continue() (which
    // would hit the real dev.ramboq.com backend). This spec doesn't
    // exercise the GTT chip — see orderbook_gtt_chip.spec.js.
    if (url.includes('/orders/gtts')) {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ gtts: [], count: 0 }),
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
    // 6, not 5 (audit fix, 2026-10) — the "Held/Attn" chip (HELD /
    // CANCEL_FAILED) was added to .ob-status-bar; see
    // order_book_held_attention_chip.spec.js for the chip's own coverage.
    //
    // 2026-10-23 fix rewrites this test's own assertions: the row is now a
    // horizontally-scrolling flex strip, not a fixed 6-column grid, so
    // "each chip's right edge sits inside the bar's current box" is no
    // longer the right invariant — a chip is explicitly ALLOWED to sit
    // past the bar's initially-visible width now; that's the whole point
    // of making it scrollable. What must hold instead: (a) the chip's
    // own LABEL text is never ellipsis-clipped (full text, readable), and
    // (b) the PAGE itself never gains horizontal scroll from this row —
    // the row's own `overflow-x: auto` must contain it.
    test(`status-bar 6 chips stay readable (no ellipsis-clip) and never force page-level overflow at ${vw}px width`, async ({ page }) => {
      await page.setViewportSize({ width: vw, height: 720 });
      await authOnce(page);
      await page.clock.setFixedTime(new Date(_NOW_ISO));
      await mockOrdersEndpoints(page);

      await page.goto('/orders');
      await page.waitForLoadState('domcontentloaded');
      await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
      await page.waitForTimeout(600);

      const chips = page.locator('.ob-status-bar .ob-sc');
      await expect(chips).toHaveCount(6);

      // The document must not scroll horizontally past the viewport —
      // the row's own `overflow-x: auto` must absorb the 6 chips'
      // combined width, never leaking into page-level scroll.
      const overflowX = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth
      );
      expect(overflowX).toBeLessThanOrEqual(1);

      // "Rejected/Cancelled" (the longest label) must render FULLY —
      // `scrollWidth > clientWidth` on the label itself is the reliable
      // signal that `overflow:hidden` + `text-overflow:ellipsis` is
      // actually clipping real content, even though ellipsis visually
      // hides the cut rather than reflowing it.
      const rejCancLabel = page.locator('.ob-status-bar .ob-sc-l', { hasText: /^Rejected\/Cancelled$/ });
      await expect(rejCancLabel).toHaveCount(1);
      const [scrollW, clientW] = await rejCancLabel.evaluate(
        (el) => [el.scrollWidth, el.clientWidth]
      );
      expect(scrollW, 'Rejected/Cancelled label must not be ellipsis-clipped').toBeLessThanOrEqual(clientW);

      // The last chip (GTT) must be reachable by scrolling the row —
      // proves the row is a real scroll container, not merely wide and
      // silently cut off by an ancestor's own overflow:hidden.
      const lastChip = chips.last();
      await lastChip.scrollIntoViewIfNeeded();
      const barBox = await page.locator('.ob-status-bar').boundingBox();
      const lastBox = await lastChip.boundingBox();
      expect(lastBox.x, 'last chip (GTT) must be scrollable into view').toBeGreaterThanOrEqual(barBox.x - 1);
      expect(lastBox.x + lastBox.width, 'last chip (GTT) must be scrollable into view').toBeLessThanOrEqual(barBox.x + barBox.width + 1);
    });
  }
});
