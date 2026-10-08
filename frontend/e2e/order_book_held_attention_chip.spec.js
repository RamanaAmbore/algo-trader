/**
 * order_book_held_attention_chip.spec.js
 *
 * Covers the 2026-10 OrderBook.svelte audit fix: rows carrying status
 * HELD or CANCEL_FAILED previously matched NO `_STATUS_PREDICATES` entry
 * and were completely invisible in this view — only HELD rows ever
 * surfaced, and only in HeldOrdersCard's own separate list.
 * CANCEL_FAILED (a cancel request failed — the order MAY STILL BE LIVE
 * AT THE BROKER) was the most dangerous omission: a possibly-still-live
 * order had no visibility anywhere in the main order book.
 *
 * Fix: a new "Held/Attn" chip (`_STATUS_PREDICATES.held`) covers both
 * statuses, is counted in `_statusCounts`, sits FIRST in `CHIP_ORDER`
 * (outranking even Chase for default-highlight priority), and both
 * statuses are exempted from the 08:00 IST session-boundary filter the
 * same way OPEN/TRIGGER_PENDING already are.
 *
 * HELD and CANCEL_FAILED are AlgoOrder-only statuses (never present on a
 * bare broker book row), so both fixture rows live in the algo/recent
 * mock, matching `order_book_filled_predicate_and_mobile_overflow.spec.js`'s
 * convention for AlgoOrderInfo-shaped rows.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/order_book_held_attention_chip.spec.js --project=chromium-desktop
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
// row inside today's 08:00 IST session boundary (irrelevant to the
// always-visible assertion below, but kept deterministic like every
// sibling OrderBook spec).
const _NOW_ISO = '2026-09-30T04:30:00.000Z';

// A resting, un-held, un-failed OPEN broker row — present purely so the
// "Open" chip has a non-zero count too, proving Held/Attn's default
// priority genuinely outranks it rather than winning only because it's
// the sole non-zero chip.
const _BROKER_ROWS = [
  { order_id: 'B5001', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-OPEN',
    transaction_type: 'BUY', quantity: 50, pending_quantity: 50, filled_quantity: 0,
    price: 100, trigger_price: 0, average_price: 0, status: 'OPEN',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-30 09:00:00' },
];

// HELD and CANCEL_FAILED are AlgoOrder-only statuses — never on a bare
// broker row. `created_at` for the HELD row is deliberately from a PRIOR
// trading session (2026-09-29, before the 08:00 IST boundary) to prove
// the session filter no longer drops it, matching OPEN's existing
// never-dropped treatment.
const _ALGO_ROWS = [
  { id: 201, account: 'T1', symbol: 'RBQ-HELDROW', exchange: 'NFO', transaction_type: 'SELL',
    quantity: 50, initial_price: 100, status: 'HELD', engine: 'live', mode: 'live',
    broker_order_id: '', hold_json: '{"category":"expiry_close","reason":"expiry review"}',
    detail: 'HELD: expiry review', created_at: '2026-09-29T03:00:00' },
  { id: 202, account: 'T1', symbol: 'RBQ-CANCELFAILROW', exchange: 'NFO', transaction_type: 'BUY',
    quantity: 50, initial_price: 100, status: 'CANCEL_FAILED', engine: 'live', mode: 'live',
    broker_order_id: 'B9999', detail: 'cancel attempt failed — order may still be live at broker',
    created_at: '2026-09-30T04:00:00' },
];

async function mockEndpoints(page) {
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

test.describe('OrderBook — Held/Attention chip (HELD + CANCEL_FAILED)', () => {
  test.setTimeout(60_000);

  test('Held/Attn chip shows combined count and is the default-highlighted chip over Open', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    const heldChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Held\/Attn$/ }) });
    await expect(heldChip).toHaveCount(1);
    await expect(heldChip.locator('.ob-sc-n')).toHaveText('2');
    await expect(heldChip).toHaveAttribute('data-status', 'held');

    // Default (nothing clicked) highlights Held/Attn, not Open — even
    // though Open also has a non-zero count — because `held` sits first
    // in CHIP_ORDER.
    await expect(heldChip).toHaveClass(/is-active/);
    await expect(page.locator('.ob-status-bar .ob-sc.is-active')).toHaveCount(1);
    const openChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Open$/ }) });
    await expect(openChip).not.toHaveClass(/is-active/);

    // Both HELD and CANCEL_FAILED rows render under the active chip.
    await expect(page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-HELDROW' })).toHaveCount(1);
    await expect(page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-CANCELFAILROW' })).toHaveCount(1);
  });

  test('HELD row from a PRIOR trading session is still visible — not dropped by the session filter', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    const heldChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Held\/Attn$/ }) });
    await heldChip.click();

    // RBQ-HELDROW's created_at (2026-09-29, prior session) would be
    // dropped by `_isCurrentSessionRow` if HELD weren't exempted the
    // same way OPEN/TRIGGER_PENDING already are.
    await expect(page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-HELDROW' })).toHaveCount(1);
  });

  test('CANCEL_FAILED row shows the ⚠ KILL FAILED pill text and a Reconcile action', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    const heldChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Held\/Attn$/ }) });
    await heldChip.click();

    const card = page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-CANCELFAILROW' });
    await expect(card).toHaveCount(1);
    await expect(card.locator('.algo-status-pill', { hasText: '⚠ KILL FAILED' })).toHaveCount(1);
    await expect(card.locator('button[aria-label="Reconcile"]')).toHaveCount(1);
  });
});
