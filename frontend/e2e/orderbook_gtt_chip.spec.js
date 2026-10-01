/**
 * orderbook_gtt_chip.spec.js
 *
 * Covers the 2026-09-30 OrderBook.svelte status-chip consolidation:
 *
 *  1. Rejected + Cancelled merged into one chip (`rejected_cancelled`,
 *     label "Rejected/Cancelled") — combined count, red/error styling
 *     (reuses the old Rejected chip's `data-status="error"` treatment —
 *     operator's explicit call on the merged-chip color).
 *  2. A new "GTT" chip at the end of the row, showing a live count of
 *     standalone broker GTT orders (GET /api/orders/gtts/ — a DIFFERENT
 *     concept from the per-filled-order attached-exit-GTT bits already
 *     rendered by OrderCard via `attached_gtts_json`).
 *  3. Default (nothing clicked) behaviour: a FIXED chip order
 *     (chase, open, filled, rejected/cancelled, gtt) — the single first
 *     chip in that order whose count is non-zero is highlighted AND is
 *     the only one whose rows render below. Not a union of every
 *     non-zero chip.
 *  4. Explicit click still exclusively filters to that one status,
 *     unchanged from before.
 *  5. The default is fully reactive — a live status change that zeroes
 *     out the currently-shown default chip's count moves the highlight
 *     + list to the next non-zero chip on the next poll tick, no reload.
 *
 * Mocks GET /api/orders/, GET /api/orders/algo/recent, and
 * GET /api/orders/gtts/ with deterministic data; freezes Date.now() via
 * page.clock.setFixedTime (NOT .install, so visibleInterval's real
 * setInterval poll keeps ticking in wall-clock time — same convention as
 * order_book_chase_chip_and_session_reset.spec.js).
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/orderbook_gtt_chip.spec.js --project=chromium-desktop
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
// row inside today's 08:00 IST session boundary.
const _NOW_ISO = '2026-09-30T04:30:00.000Z';

// ── Fixture A — static, for the merged-chip + GTT-chip-rendering tests ──
const _BROKER_ROWS_A = [
  { order_id: 'B3001', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-REJ',
    transaction_type: 'BUY', quantity: 50, pending_quantity: 0, filled_quantity: 0,
    price: 100, trigger_price: 0, average_price: 0, status: 'REJECTED',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-30 09:05:00' },
  { order_id: 'B3002', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-CXL',
    transaction_type: 'BUY', quantity: 50, pending_quantity: 0, filled_quantity: 0,
    price: 100, trigger_price: 0, average_price: 0, status: 'CANCELLED',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-30 09:10:00' },
];
const _ALGO_ROWS_A = [];

const _GTT_ROWS_A = [
  { gtt_id: '9001', account: 'T1', broker_id: 'kite', status: 'active',
    trigger_type: 'single', tradingsymbol: 'RBQ-GTT1', exchange: 'NSE',
    trigger_values: [101.5], last_price: 100.2, orders: [], created_at: '2026-09-30T08:30:00' },
  { gtt_id: '9002', account: 'T1', broker_id: 'kite', status: 'active',
    trigger_type: 'two-leg', tradingsymbol: 'RBQ-GTT2', exchange: 'NFO',
    trigger_values: [95, 110], last_price: 100.2, orders: [], created_at: '2026-09-30T08:35:00' },
  // Terminal GTT — must NOT count towards the chip or render in the list.
  { gtt_id: '9003', account: 'T1', broker_id: 'kite', status: 'triggered',
    trigger_type: 'single', tradingsymbol: 'RBQ-GTT-DEAD', exchange: 'NSE',
    trigger_values: [50], last_price: 100.2, orders: [], created_at: '2026-09-29T08:30:00' },
];

async function mockEndpointsA(page) {
  await page.route('**/api/orders/**', async (route) => {
    const req = route.request();
    if (req.method() !== 'GET') { await route.continue(); return; }
    const url = req.url();
    if (url.includes('/orders/gtts')) {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ gtts: _GTT_ROWS_A, count: _GTT_ROWS_A.length }),
      });
      return;
    }
    if (url.includes('/orders/algo/recent')) {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify(_ALGO_ROWS_A),
      });
      return;
    }
    if (/\/api\/orders\/?(\?.*)?$/.test(url)) {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: _BROKER_ROWS_A, refreshed_at: new Date().toISOString() }),
      });
      return;
    }
    await route.continue();
  });
}

test.describe('OrderBook — Rejected/Cancelled merge + GTT chip', () => {
  test.setTimeout(60_000);

  test('Rejected/Cancelled chip shows combined count with red/error styling', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockEndpointsA(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    await expect(page.locator('.ob-status-bar .ob-sc-l', { hasText: /^Rejected$/ })).toHaveCount(0);
    await expect(page.locator('.ob-status-bar .ob-sc-l', { hasText: /^Cancelled$/ })).toHaveCount(0);

    const mergedChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Rejected\/Cancelled$/ }) });
    await expect(mergedChip).toHaveCount(1);
    await expect(mergedChip.locator('.ob-sc-n')).toHaveText('2');
    await expect(mergedChip).toHaveAttribute('data-status', 'error');
  });

  test('GTT chip renders and reflects its live (non-terminal) count', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockEndpointsA(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    const gttChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^GTT$/ }) });
    await expect(gttChip).toHaveCount(1);
    // 2, not 3 — the 'triggered' row is terminal and excluded from the
    // live count.
    await expect(gttChip.locator('.ob-sc-n')).toHaveText('2');

    await gttChip.click();
    const gttCards = page.locator('.gtt-card');
    await expect(gttCards).toHaveCount(2);
    await expect(gttCards.filter({ hasText: 'RBQ-GTT1' })).toHaveCount(1);
    await expect(gttCards.filter({ hasText: 'RBQ-GTT2' })).toHaveCount(1);
    await expect(gttCards.filter({ hasText: 'RBQ-GTT-DEAD' })).toHaveCount(0);

    // Each live GTT card shows a Cancel action.
    await expect(gttCards.first().locator('button[aria-label="Cancel"]')).toHaveCount(1);
  });
});

// ── Fixture B — default-chip + reactivity tests ─────────────────────────
// Starts with ONLY Open rows (chase=0, open=2, complete=0,
// rejected_cancelled=0, gtt=0) so the default resolves to 'open' — the
// 2nd entry in CHIP_ORDER, deliberately NOT the 1st, to prove the default
// logic isn't hardcoded to always pick the first chip in the array.
function _makeOpenRows() {
  return [
    { order_id: 'B4001', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-OPEN1',
      transaction_type: 'BUY', quantity: 50, pending_quantity: 50, filled_quantity: 0,
      price: 100, trigger_price: 0, average_price: 0, status: 'OPEN',
      order_type: 'LIMIT', product: 'MIS', variety: 'regular',
      order_timestamp: '2026-09-30 09:00:00' },
    { order_id: 'B4002', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-OPEN2',
      transaction_type: 'SELL', quantity: 50, pending_quantity: 50, filled_quantity: 0,
      price: 100, trigger_price: 0, average_price: 0, status: 'OPEN',
      order_type: 'LIMIT', product: 'MIS', variety: 'regular',
      order_timestamp: '2026-09-30 09:05:00' },
  ];
}
function _makeFilledRows() {
  return [
    { order_id: 'B4001', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-OPEN1',
      transaction_type: 'BUY', quantity: 50, pending_quantity: 0, filled_quantity: 50,
      price: 100, trigger_price: 0, average_price: 100, status: 'COMPLETE',
      order_type: 'LIMIT', product: 'MIS', variety: 'regular',
      order_timestamp: '2026-09-30 09:00:00' },
    { order_id: 'B4002', account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-OPEN2',
      transaction_type: 'SELL', quantity: 50, pending_quantity: 0, filled_quantity: 50,
      price: 100, trigger_price: 0, average_price: 100, status: 'COMPLETE',
      order_type: 'LIMIT', product: 'MIS', variety: 'regular',
      order_timestamp: '2026-09-30 09:05:00' },
  ];
}

/** Mounts routes backed by mutable `state.brokerRows` / `state.gttRows` so
 *  a test can flip the fixture mid-run and observe the next poll tick. */
async function mockEndpointsMutable(page, state) {
  await page.route('**/api/orders/**', async (route) => {
    const req = route.request();
    if (req.method() !== 'GET') { await route.continue(); return; }
    const url = req.url();
    if (url.includes('/orders/gtts')) {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ gtts: state.gttRows, count: state.gttRows.length }),
      });
      return;
    }
    if (url.includes('/orders/algo/recent')) {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify([]),
      });
      return;
    }
    if (/\/api\/orders\/?(\?.*)?$/.test(url)) {
      // Always HTTP 200 — a non-200/network error would freeze to
      // last-known-good (OrderBook's staleness-freeze convention), which
      // would defeat this test's "live status change" premise.
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: state.brokerRows, refreshed_at: new Date().toISOString() }),
      });
      return;
    }
    await route.continue();
  });
}

test.describe('OrderBook — single-chip default + reactivity', () => {
  test.setTimeout(60_000);

  test('Default (nothing clicked) highlights ONLY the first non-zero chip — Open, not a union', async ({ page }) => {
    const state = { brokerRows: _makeOpenRows(), gttRows: [] };
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockEndpointsMutable(page, state);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    // Exactly one chip carries .is-active, and it's Open.
    await expect(page.locator('.ob-status-bar .ob-sc.is-active')).toHaveCount(1);
    const openChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Open$/ }) });
    await expect(openChip).toHaveClass(/is-active/);
    const chaseChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Chase$/ }) });
    await expect(chaseChip).not.toHaveClass(/is-active/);

    // ...and the grid shows only Open's 2 rows.
    await expect(page.locator('.oc-book-grid .order-card')).toHaveCount(2);
  });

  test('Explicit click still exclusively filters to one status (unchanged behaviour)', async ({ page }) => {
    const state = { brokerRows: _makeOpenRows(), gttRows: [] };
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockEndpointsMutable(page, state);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    // Default is Open (2 cards) — click Filled (count 0) explicitly.
    const filledChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Filled$/ }) });
    await filledChip.click();
    await expect(filledChip).toHaveClass(/is-active/);
    await expect(page.locator('.ob-status-bar .ob-sc.is-active')).toHaveCount(1);
    await expect(page.locator('.oc-book-grid .order-card')).toHaveCount(0);
  });

  test('Live status change: Open emptying moves the default highlight + list to Filled, no reload', async ({ page }) => {
    const state = { brokerRows: _makeOpenRows(), gttRows: [] };
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockEndpointsMutable(page, state);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    const openChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Open$/ }) });
    await expect(openChip).toHaveClass(/is-active/);
    await expect(page.locator('.oc-book-grid .order-card')).toHaveCount(2);

    // Flip the fixture: both rows transition OPEN → COMPLETE (Open count
    // drops to 0, Filled count becomes 2). OrderBook's own poll
    // (pollMs=3000 default on /orders) picks this up on the next tick —
    // no page reload, no re-click.
    state.brokerRows = _makeFilledRows();

    const filledChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Filled$/ }) });
    await expect(filledChip).toHaveClass(/is-active/, { timeout: 7_000 });
    await expect(openChip).not.toHaveClass(/is-active/);
    await expect(page.locator('.ob-status-bar .ob-sc.is-active')).toHaveCount(1);
    await expect(page.locator('.oc-book-grid .order-card')).toHaveCount(2);
    await expect(page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-OPEN1' })).toHaveCount(1);
  });

  test('Live status change: all orders clear while a GTT appears — default moves to the GTT list', async ({ page }) => {
    const state = { brokerRows: _makeOpenRows(), gttRows: [] };
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockEndpointsMutable(page, state);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    const openChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Open$/ }) });
    await expect(openChip).toHaveClass(/is-active/);

    // Flip the fixture: no more orders at all (any status), one active
    // GTT appears. Every order chip's count goes to 0; GTT becomes the
    // only non-zero chip and the new default.
    state.brokerRows = [];
    state.gttRows = [
      { gtt_id: '9101', account: 'T1', broker_id: 'kite', status: 'active',
        trigger_type: 'single', tradingsymbol: 'RBQ-LIVEGTT', exchange: 'NSE',
        trigger_values: [123.4], last_price: 120, orders: [], created_at: '2026-09-30T09:00:00' },
    ];

    const gttChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^GTT$/ }) });
    await expect(gttChip).toHaveClass(/is-active/, { timeout: 7_000 });
    await expect(page.locator('.ob-status-bar .ob-sc.is-active')).toHaveCount(1);
    await expect(page.locator('.oc-book-grid .order-card')).toHaveCount(0);
    await expect(page.locator('.gtt-card')).toHaveCount(1);
    await expect(page.locator('.gtt-card', { hasText: 'RBQ-LIVEGTT' })).toHaveCount(1);
  });
});
