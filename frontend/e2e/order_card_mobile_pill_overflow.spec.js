/**
 * order_card_mobile_pill_overflow.spec.js
 *
 * Operator-reported (confirmed live via Playwright at 360×800): an
 * OrderCard inside OrderBook's grid could be WIDER than its container
 * (card right edge measured past the 360px viewport), clipping the
 * status pill (REJECTED/CANCELLED/etc., top-right of the card) off-screen.
 *
 * Root cause: `.ob-scroll .oc-book-grid`'s single-column breakpoint used a
 * bare `grid-template-columns: 1fr` (shorthand for `minmax(auto, 1fr)`).
 * A bare `1fr` track floors at the grid ITEM's min-content width — for an
 * OrderCard that's the unwrapped max-content width of its top summary row
 * (txn-type + account + symbol + status pill). The symbol span's own
 * `min-w-0 truncate` can never free up room until the TRACK ITSELF is
 * allowed to shrink below that floor, so a sufficiently long symbol +
 * account forced the whole card (and its grid cell) wider than the
 * viewport, taking the trailing `ml-auto` status pill off-screen with it.
 *
 * Fix: `grid-template-columns: minmax(0, 1fr)` (matching the existing
 * 640px/1024px multi-column breakpoints, which already had the minmax)
 * plus a `min-width: 0` backstop directly on the grid-item classes
 * (`.order-card`, `.gtt-card`), mirroring LogPanel's own
 * `.lp-multicol .log-row { min-width: 0 }` convention for the identical
 * CSS-grid intrinsic-sizing trap.
 *
 * Uses a long symbol + account (matching the "sufficiently long content"
 * shape that reproduced the live bug) so the top row's min-content
 * genuinely exceeds a 360px viewport if the track isn't actually
 * constrained — a short symbol would pass trivially even on the
 * unfixed grid and prove nothing.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/order_card_mobile_pill_overflow.spec.js --project=chromium-desktop
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

// Fixed "now" = 2026-09-30 10:00 IST — keeps every row inside today's
// 08:00 IST session boundary (unrelated session filter), matching the
// convention used by every sibling OrderBook spec.
const _NOW_ISO = '2026-09-30T04:30:00.000Z';

// Long account + long F&O symbol — the content shape that reproduced the
// live overflow. `average_price` deliberately omitted (not 0) so the
// price chip doesn't collapse to "0.00" (see
// order_book_broker_order_id_dedup.spec.js's own comment on this quirk) —
// not load-bearing for this spec, but keeps the card's rendered content
// realistic.
const _BROKER_ROWS = [
  { order_id: 'B7001', account: 'ZG9999999', exchange: 'NFO', tradingsymbol: 'BANKNIFTY26OCT54000CE',
    transaction_type: 'SELL', quantity: 25, pending_quantity: 0, filled_quantity: 0,
    price: 450, trigger_price: 0, status: 'REJECTED',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-30 09:05:00' },
  { order_id: 'B7002', account: 'ZG9999999', exchange: 'NFO', tradingsymbol: 'BANKNIFTY26OCT54000CE',
    transaction_type: 'SELL', quantity: 25, pending_quantity: 25, filled_quantity: 0,
    price: 452, trigger_price: 0, status: 'CANCELLED',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-30 09:06:00' },
];

async function mockOrdersEndpoints(page) {
  await page.route('**/api/orders/**', async (route) => {
    const req = route.request();
    if (req.method() !== 'GET') { await route.continue(); return; }
    const url = req.url();
    if (url.includes('/orders/gtts')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ gtts: [], count: 0 }) });
      return;
    }
    if (url.includes('/orders/chases/active')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
      return;
    }
    if (url.includes('/orders/algo/recent')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
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

test.describe('OrderCard — status pill stays visible at 360px (2026-10-23 grid-track fix)', () => {
  test.setTimeout(60_000);

  test('REJECTED and CANCELLED cards keep their status pill fully inside the 360px viewport', async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 800 });
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockOrdersEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });

    // Both fixture rows are REJECTED/CANCELLED — click the merged chip so
    // both render (default-chip resolution would already land here since
    // it's the only non-zero bucket, but click explicitly for clarity and
    // robustness against default-resolution changes).
    const chip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Rejected\/Cancelled$/ }) });
    await chip.click();

    // Match on account text (not the symbol) — `formatSymbol()` may
    // re-hyphenate a parseable F&O symbol for display (e.g.
    // "BANKNIFTY-26OCT-54000-CE"), so a raw-symbol substring match is
    // unreliable. Both fixture rows are the only two cards this mock
    // ever produces.
    const cards = page.locator('.oc-book-grid .order-card', { hasText: 'ZG9999999' });
    await expect(cards).toHaveCount(2);

    const vw = 360;
    for (let i = 0; i < 2; i++) {
      const card = cards.nth(i);
      const cardBox = await card.boundingBox();
      expect(cardBox.x + cardBox.width, `card ${i} right edge within viewport`).toBeLessThanOrEqual(vw + 1);

      const pill = card.locator('.algo-status-pill');
      await expect(pill).toHaveCount(1);
      const pillBox = await pill.boundingBox();
      expect(pillBox, `card ${i} status pill must have a measurable bounding box (not off-screen/display:none)`).not.toBeNull();
      expect(pillBox.x, `card ${i} status pill left edge within viewport`).toBeGreaterThanOrEqual(0);
      expect(pillBox.x + pillBox.width, `card ${i} status pill right edge within viewport`).toBeLessThanOrEqual(vw + 1);
    }

    // Document-level backstop — no card should force page-level horizontal
    // scroll on a 360px viewport.
    const overflowX = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflowX).toBeLessThanOrEqual(1);
  });
});
