/**
 * order_card_status_pill_equal_width.spec.js
 *
 * Operator-reported: order status cards (the `.algo-status-pill` badge
 * rendered inside OrderCard.svelte / OrderBook.svelte GTT cards) had no
 * width floor and auto-sized to their own text — OPEN (4 chars) rendered
 * visibly narrower than CANCELLED (9 chars), REJECTED (8 chars), or the
 * CANCEL_FAILED pill's "⚠ KILL FAILED" label (longest common case).
 *
 * Fix: `.algo-status-pill` (app.css) now carries a `min-width` floor +
 * `text-align: center`, so every common status renders at the SAME width
 * instead of auto-sizing. A min-width (not a rigid fixed width) was chosen
 * so a rare, much longer raw broker status still grows past the floor
 * rather than clipping (white-space:nowrap is unchanged).
 *
 * This spec mocks GET /api/orders/ on the /orders page (mounts OrderBook)
 * with one row per status, clicks each status chip in turn (OPEN/FILLED
 * and CANCELLED/REJECTED live in different OrderBook buckets — see
 * OrderBook.svelte's `_STATUS_PREDICATES`), and measures each rendered
 * pill's `getBoundingClientRect().width` to confirm they are now equal.
 * CANCEL_FAILED (rendered as "⚠ KILL FAILED") is included as the width-
 * defining longest common label.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/order_card_status_pill_equal_width.spec.js --project=chromium-desktop
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
// 08:00 IST session boundary, matching the convention used by every
// sibling OrderBook spec (e.g. order_card_mobile_pill_overflow.spec.js).
const _NOW_ISO = '2026-09-30T04:30:00.000Z';

const _BROKER_ROWS = [
  { order_id: 'B9001', account: 'ZG0000001', exchange: 'NSE', tradingsymbol: 'RELIANCE',
    transaction_type: 'BUY', quantity: 10, pending_quantity: 10, filled_quantity: 0,
    price: 2500, trigger_price: 0, status: 'OPEN',
    order_type: 'LIMIT', product: 'CNC', variety: 'regular',
    order_timestamp: '2026-09-30 09:05:00' },
  { order_id: 'B9002', account: 'ZG0000002', exchange: 'NSE', tradingsymbol: 'INFY',
    transaction_type: 'BUY', quantity: 10, pending_quantity: 0, filled_quantity: 10,
    price: 1500, average_price: 1501, trigger_price: 0, status: 'COMPLETE',
    order_type: 'LIMIT', product: 'CNC', variety: 'regular',
    order_timestamp: '2026-09-30 09:06:00' },
  { order_id: 'B9003', account: 'ZG0000003', exchange: 'NFO', tradingsymbol: 'BANKNIFTY26OCT54000CE',
    transaction_type: 'SELL', quantity: 25, pending_quantity: 25, filled_quantity: 0,
    price: 452, trigger_price: 0, status: 'CANCELLED',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-30 09:07:00' },
  { order_id: 'B9004', account: 'ZG0000004', exchange: 'NFO', tradingsymbol: 'BANKNIFTY26OCT54000CE',
    transaction_type: 'SELL', quantity: 25, pending_quantity: 0, filled_quantity: 0,
    price: 450, trigger_price: 0, status: 'REJECTED',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-30 09:08:00' },
  { order_id: 'B9005', account: 'ZG0000005', exchange: 'NFO', tradingsymbol: 'NIFTY26OCT24000PE',
    transaction_type: 'SELL', quantity: 50, pending_quantity: 50, filled_quantity: 0,
    price: 120, trigger_price: 0, status: 'CANCEL_FAILED',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-30 09:09:00' },
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

/** Click a status chip by its visible label and return the first pill's width. */
async function _measurePillWidthForChip(page, label, accountHint) {
  const chip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: new RegExp(`^${label}$`) }) });
  await chip.click();
  const card = page.locator('.oc-book-grid .order-card', { hasText: accountHint }).first();
  await expect(card).toBeVisible();
  const pill = card.locator('.algo-status-pill').first();
  await expect(pill).toBeVisible();
  const box = await pill.boundingBox();
  expect(box, `pill for ${label} (${accountHint}) must have a measurable bounding box`).not.toBeNull();
  return box.width;
}

test.describe('OrderCard status pill — equal width across OPEN/FILLED/CANCELLED/REJECTED/CANCEL_FAILED (2026-10-24 fix)', () => {
  test.setTimeout(60_000);

  test('every common status pill renders at the same width', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockOrdersEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });

    const widths = {
      OPEN:          await _measurePillWidthForChip(page, 'Open', 'ZG0000001'),
      FILLED:        await _measurePillWidthForChip(page, 'Filled', 'ZG0000002'),
      'CANCELLED/REJECTED': await (async () => {
        const chip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Rejected\/Cancelled$/ }) });
        await chip.click();
        const cards = page.locator('.oc-book-grid .order-card', { hasText: /ZG000000[34]/ });
        await expect(cards).toHaveCount(2);
        const w1 = (await cards.nth(0).locator('.algo-status-pill').first().boundingBox()).width;
        const w2 = (await cards.nth(1).locator('.algo-status-pill').first().boundingBox()).width;
        expect(Math.abs(w1 - w2), 'CANCELLED pill width must equal REJECTED pill width').toBeLessThanOrEqual(1.0);
        return w1;
      })(),
      CANCEL_FAILED: await _measurePillWidthForChip(page, 'Held/Attn', 'ZG0000005'),
    };

    const reference = widths.OPEN;
    for (const [status, w] of Object.entries(widths)) {
      expect(w, `${status} pill width (${w}px) must match OPEN pill width (${reference}px)`).toBeGreaterThan(0);
      expect(Math.abs(w - reference), `${status} pill width (${w}px) vs OPEN (${reference}px)`).toBeLessThanOrEqual(1.0);
    }
  });

  test('pills stay equal width and unclipped at a 360px mobile viewport', async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 800 });
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockOrdersEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });

    const openWidth = await _measurePillWidthForChip(page, 'Open', 'ZG0000001');
    const rejectedCancelledChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Rejected\/Cancelled$/ }) });
    await rejectedCancelledChip.click();
    const cancelledCard = page.locator('.oc-book-grid .order-card', { hasText: 'ZG0000003' }).first();
    await expect(cancelledCard).toBeVisible();
    const cancelledPill = cancelledCard.locator('.algo-status-pill').first();
    const cancelledBox = await cancelledPill.boundingBox();

    expect(Math.abs(cancelledBox.width - openWidth), 'CANCELLED pill width must match OPEN pill width on mobile too').toBeLessThanOrEqual(1.0);

    const cardBox = await cancelledCard.boundingBox();
    expect(cardBox.x + cardBox.width, 'card stays inside the 360px viewport').toBeLessThanOrEqual(361);
    expect(cancelledBox.x + cancelledBox.width, 'pill stays inside the 360px viewport').toBeLessThanOrEqual(361);
  });
});
