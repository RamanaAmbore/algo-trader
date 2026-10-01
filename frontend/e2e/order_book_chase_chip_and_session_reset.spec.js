/**
 * order_book_chase_chip_and_session_reset.spec.js
 *
 * Covers two OrderBook.svelte changes:
 *
 *  1. The "All" status chip is replaced with a "Chase" chip — orders the
 *     chase engine currently has in flight (status OPEN/TRIGGER_PENDING
 *     AND `attempts > 0`; a plain resting order the chase engine has
 *     never touched does NOT count, matching OrderCard.svelte's existing
 *     `chase:#N` chip precedent).
 *
 *  2. Prior-trading-session TERMINAL orders (before today's 08:00 IST
 *     boundary) are dropped from the Order Book entirely — from the grid
 *     AND from every status chip count, since both read the same session-
 *     filtered `orderRows`. Today's terminal rows (filled/rejected/
 *     cancelled) still show; only terminal rows from a PRIOR session
 *     vanish. A still-OPEN row from a prior session is NEVER dropped by
 *     this filter, regardless of age — operator explicit instruction
 *     (2026-09-30, reversing the initial default): "keep them visible
 *     until reconciled" (see OrderBook.svelte's `_isCurrentSessionRow`
 *     call-site comment).
 *
 * Mocks GET /api/orders/ (broker book) and GET /api/orders/algo/recent
 * (algo-tracked book) with a fixed, deterministic dataset, and freezes
 * the page clock's Date.now()/new Date() (but NOT setTimeout/setInterval
 * — page.clock.setFixedTime, not .install — so visibleInterval's real
 * poll timers keep working normally) to a fixed "now" so the 08:00 IST
 * session-boundary math is deterministic regardless of when the test
 * actually runs.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/order_book_chase_chip_and_session_reset.spec.js --project=chromium-desktop
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

// Fixed "now" = 2026-09-30 10:00 IST = 2026-09-30T04:30:00Z. Today's
// 08:00 IST session boundary = 2026-09-30T02:30:00Z.
const _NOW_ISO = '2026-09-30T04:30:00.000Z';

// ── Broker book (/api/orders/ — OrderRow shape) ─────────────────────────
const _BROKER_ROWS = [
  { order_id: 'B1001', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-FRESHOPEN',
    transaction_type: 'BUY', quantity: 50, pending_quantity: 50, filled_quantity: 0,
    price: 100, trigger_price: 0, average_price: 0, status: 'OPEN',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-30 09:15:00' },
  { order_id: 'B1002', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-FRESHFILL',
    transaction_type: 'SELL', quantity: 50, pending_quantity: 0, filled_quantity: 50,
    price: 100, trigger_price: 0, average_price: 101, status: 'COMPLETE',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-30 09:20:00' },
  { order_id: 'B1003', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-FRESHREJ',
    transaction_type: 'BUY', quantity: 50, pending_quantity: 0, filled_quantity: 0,
    price: 100, trigger_price: 0, average_price: 0, status: 'REJECTED',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-30 09:05:00' },
  { order_id: 'B1004', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-FRESHCXL',
    transaction_type: 'BUY', quantity: 50, pending_quantity: 0, filled_quantity: 0,
    price: 100, trigger_price: 0, average_price: 0, status: 'CANCELLED',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-30 09:10:00' },
  // Prior session (yesterday) — must be dropped entirely.
  { order_id: 'B1005', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-STALEFILL',
    transaction_type: 'SELL', quantity: 50, pending_quantity: 0, filled_quantity: 50,
    price: 100, trigger_price: 0, average_price: 101, status: 'COMPLETE',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-29 15:30:00' },
  // Prior session, still OPEN — NEVER hidden by the session filter (see spec docstring).
  { order_id: 'B1006', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-STALEOPEN',
    transaction_type: 'BUY', quantity: 50, pending_quantity: 50, filled_quantity: 0,
    price: 100, trigger_price: 0, average_price: 0, status: 'OPEN',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-29 18:00:00' },
];

// ── Algo book (/api/orders/algo/recent — AlgoOrderInfo shape) ──────────
// created_at mirrors Python's naive-UTC datetime.isoformat() (no 'Z'/offset).
const _ALGO_ROWS = [
  { id: 1, account: 'T1', symbol: 'RBQ-CHASE1', exchange: 'NFO', transaction_type: 'BUY',
    quantity: 50, initial_price: 100, current_limit: 102, fill_price: null,
    attempts: 3, status: 'OPEN', engine: 'chase', mode: 'live', detail: null,
    created_at: '2026-09-30T04:00:00' },
  { id: 2, account: 'T1', symbol: 'RBQ-CHASE2NOATT', exchange: 'NFO', transaction_type: 'BUY',
    quantity: 50, initial_price: 100, current_limit: 100, fill_price: null,
    attempts: 0, status: 'OPEN', engine: 'chase', mode: 'live', detail: null,
    created_at: '2026-09-30T04:10:00' },
  { id: 3, account: 'T1', symbol: 'RBQ-CHASE3TERM', exchange: 'NFO', transaction_type: 'BUY',
    quantity: 50, initial_price: 100, current_limit: 100, fill_price: 101,
    attempts: 2, status: 'COMPLETE', engine: 'chase', mode: 'live', detail: null,
    created_at: '2026-09-30T03:50:00' },
  // Prior session, still OPEN + attempts > 0 — NEVER hidden (still-working
  // rows are exempt from the session filter) and DOES count as Chase.
  { id: 4, account: 'T1', symbol: 'RBQ-STALECHASE', exchange: 'NFO', transaction_type: 'BUY',
    quantity: 50, initial_price: 100, current_limit: 105, fill_price: null,
    attempts: 5, status: 'OPEN', engine: 'chase', mode: 'live', detail: null,
    created_at: '2026-09-29T09:00:00' },
];

async function mockOrdersEndpoints(page) {
  await page.route('**/api/orders/**', async (route) => {
    const req = route.request();
    if (req.method() !== 'GET') { await route.continue(); return; }
    const url = req.url();
    // GTT list — checked before the bare-/orders/ regex below so it never
    // falls through to route.continue() (which would hit the real
    // dev.ramboq.com backend and 404, since this endpoint isn't deployed
    // there yet). Deterministic empty set — this spec covers the order
    // chips only; see orderbook_gtt_chip.spec.js for GTT coverage.
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

test.describe('OrderBook — Chase chip + session-boundary reset', () => {
  test.setTimeout(60_000);

  test('All chip removed, Chase chip present with correct in-flight count; Rejected+Cancelled merged', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockOrdersEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    // Let the initial _loadOrders() resolve.
    await page.waitForTimeout(600);

    const chips = page.locator('.ob-status-bar .ob-sc');
    await expect(chips).toHaveCount(5);

    // No "All" chip anywhere in the strip.
    await expect(page.locator('.ob-status-bar .ob-sc-l', { hasText: /^All$/ })).toHaveCount(0);
    // No separate Rejected / Cancelled chips — merged into one.
    await expect(page.locator('.ob-status-bar .ob-sc-l', { hasText: /^Rejected$/ })).toHaveCount(0);
    await expect(page.locator('.ob-status-bar .ob-sc-l', { hasText: /^Cancelled$/ })).toHaveCount(0);

    // Exactly one "Chase" chip, count = 2: id=1 (OPEN + attempts>0, today's
    // session) AND id=4/RBQ-STALECHASE (OPEN + attempts>0, prior session —
    // still-working rows are exempt from the session filter).
    const chaseChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Chase$/ }) });
    await expect(chaseChip).toHaveCount(1);
    await expect(chaseChip.locator('.ob-sc-n')).toHaveText('2');

    // Open count = 5: B1001 + algo id1 + algo id2 (today's session) +
    // B1006/RBQ-STALEOPEN + algo id4/RBQ-STALECHASE (prior session, both
    // still OPEN, both exempt from the session filter — Chase is a SUBSET
    // of Open, not mutually exclusive, so id4 counts in both chips).
    // Complete = 2; combined Rejected/Cancelled = 1 + 1 = 2.
    const openChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Open$/ }) });
    await expect(openChip.locator('.ob-sc-n')).toHaveText('5');
    const completeChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Filled$/ }) });
    await expect(completeChip.locator('.ob-sc-n')).toHaveText('2');
    // Merged chip — single chip, combined count, red/error styling.
    const mergedChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Rejected\/Cancelled$/ }) });
    await expect(mergedChip).toHaveCount(1);
    await expect(mergedChip.locator('.ob-sc-n')).toHaveText('2');
    await expect(mergedChip).toHaveAttribute('data-status', 'error');

    // GTT chip present too (mocked to zero in this spec — see
    // orderbook_gtt_chip.spec.js for GTT-specific coverage).
    const gttChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^GTT$/ }) });
    await expect(gttChip).toHaveCount(1);
    await expect(gttChip.locator('.ob-sc-n')).toHaveText('0');
  });

  test('Default view (nothing clicked) highlights ONLY Chase — the first non-zero chip in order', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockOrdersEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    // CHIP_ORDER = ['chase','open','complete','rejected_cancelled','gtt'].
    // Chase has a non-zero count (2) in this fixture, so it's first-non-
    // zero and becomes the default — not Open, even though Open is also
    // non-zero (5). Exactly ONE chip carries .is-active.
    const activeChips = page.locator('.ob-status-bar .ob-sc.is-active');
    await expect(activeChips).toHaveCount(1);
    const chaseChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Chase$/ }) });
    await expect(chaseChip).toHaveClass(/is-active/);

    // ...and the grid below shows only Chase's 2 rows, matching the chip.
    await expect(page.locator('.oc-book-grid .order-card')).toHaveCount(2);
    await expect(page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-CHASE1' })).toHaveCount(1);
    await expect(page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-STALECHASE' })).toHaveCount(1);
  });

  test('Clicking Chase filters the grid to only in-flight-chased rows (explicit click unchanged)', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockOrdersEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    const chaseChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Chase$/ }) });
    // Default already resolves to Chase in this fixture (see previous
    // test) — click it anyway to exercise the explicit-click path
    // (_internalStatus becomes non-null, sticky from here on).
    await chaseChip.click();

    const cards = page.locator('.oc-book-grid .order-card');
    await expect(cards).toHaveCount(2);
    await expect(page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-CHASE1' })).toHaveCount(1);
    // Prior-session still-OPEN + attempts>0 row DOES count as chase now.
    await expect(page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-STALECHASE' })).toHaveCount(1);
    // Terminal row with attempts>0 must NOT be counted as chase.
    await expect(page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-CHASE3TERM' })).toHaveCount(0);
    // OPEN row with attempts=0 must NOT be counted as chase.
    await expect(page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-CHASE2NOATT' })).toHaveCount(0);
  });

  test('Prior-session TERMINAL orders never appear, but a prior-session still-OPEN row stays visible until reconciled', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockOrdersEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    // Terminal stale row never renders, regardless of which status chip
    // is active.
    for (const filterLabel of [/^Open$/, /^Filled$/, /^Rejected\/Cancelled$/, /^Chase$/]) {
      const chip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: filterLabel }) });
      await chip.click();
      await expect(page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-STALEFILL' })).toHaveCount(0);
    }

    // A fresh today's-session row (from BEFORE and AFTER the 08:00 IST
    // boundary within today) does appear under Open.
    const openChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Open$/ }) });
    await openChip.click();
    await expect(page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-FRESHOPEN' })).toHaveCount(1);

    // The prior-session still-OPEN row stays visible under Open — operator
    // instruction: "keep them visible until reconciled."
    await expect(page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-STALEOPEN' })).toHaveCount(1);

    // ...and its chase-tracked sibling (attempts>0) shows under Chase too.
    const chaseChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Chase$/ }) });
    await chaseChip.click();
    await expect(page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-STALECHASE' })).toHaveCount(1);
  });
});
