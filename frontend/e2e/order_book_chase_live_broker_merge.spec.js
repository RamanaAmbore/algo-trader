/**
 * order_book_chase_live_broker_merge.spec.js
 *
 * Regression guard for plan `starry-tickling-dolphin`, frontend item 1/2:
 * OrderBook.svelte's merge let the broker row win over its matching algo
 * row once BOTH existed (a REAL live chase whose order has actually
 * reached the broker book) — and a bare broker row carries no `attempts`/
 * `current_limit`/`next_attempt_at` fields at all. So the "Chase in
 * flight" chip (`_isChaseInFlight`, gated on `attempts > 0`) and
 * OrderCard's `chase:#N` marker only ever fired for a paper/sim/shadow
 * order (which never has a broker-row counterpart and so always stays in
 * `algoOnly`, keeping its own `attempts` field intact) — never for a real
 * live chase.
 *
 * This spec mocks a chase that HAS reached the broker book: a broker row
 * (`/api/orders/`) and an algo row (`/api/orders/algo/recent`) sharing the
 * same `broker_order_id`, with the algo row carrying `attempts: 4`,
 * `mode: 'live'` — the exact shape a real live chase produces. Confirmed
 * (by direct code reading before the fix) to fail against the pre-fix
 * merge: the merged row keeps `_rowOrigin: 'broker'` with no `attempts`
 * field, so `_isChaseInFlight` reads `Number(undefined || 0) > 0` ===
 * false, the Chase chip count is 0, and OrderCard's `chase:#N` chip never
 * renders for this row.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/order_book_chase_live_broker_merge.spec.js --project=chromium-desktop
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

// Fixed "now" = 2026-09-30 10:00 IST — same convention as the sibling spec
// (order_book_chase_chip_and_session_reset.spec.js) so both order_timestamp
// values below land safely inside today's trading session.
const _NOW_ISO = '2026-09-30T04:30:00.000Z';

const _LIVE_BROKER_ORDER_ID = 'B9001';

// ── Broker book — the chase's order has reached the broker book, still
// resting OPEN there (a fresh re-quote, not yet cancelled-and-replaced
// again this poll). ──────────────────────────────────────────────────────
const _BROKER_ROWS = [
  { order_id: _LIVE_BROKER_ORDER_ID, account: 'T1', exchange: 'NFO', tradingsymbol: 'RBQ-LIVECHASE',
    transaction_type: 'BUY', quantity: 50, pending_quantity: 50, filled_quantity: 0,
    price: 103, trigger_price: 0, average_price: 0, status: 'OPEN',
    order_type: 'LIMIT', product: 'MIS', variety: 'regular',
    order_timestamp: '2026-09-30 09:15:00' },
];

// ── Algo book — same row, tracked by the LIVE chase engine, carrying the
// chase bookkeeping fields a real live chase actually writes
// (attempts/current_limit/next_attempt_at via _sync_algo_order_id).
// `broker_order_id` is the shared identity key the merge dedups on. ──────
const _ALGO_ROWS = [
  { id: 101, account: 'T1', symbol: 'RBQ-LIVECHASE', exchange: 'NFO', transaction_type: 'BUY',
    quantity: 50, initial_price: 100, current_limit: 103, fill_price: null,
    attempts: 4, status: 'OPEN', engine: 'chase', mode: 'live', detail: null,
    created_at: '2026-09-30T04:10:00', broker_order_id: _LIVE_BROKER_ORDER_ID },
];

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

test.describe('OrderBook — chase chip fires for a real LIVE chase (broker-matched row)', () => {
  test.setTimeout(60_000);

  test('Chase chip counts a live order whose broker row and algo row share broker_order_id', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockOrdersEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    // Chase chip count = 1 — this is the actual regression guard: pre-fix,
    // the merged row (broker-origin, no `attempts` field copied over)
    // would read `_isChaseInFlight` as false and this count would be 0.
    const chaseChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Chase$/ }) });
    await expect(chaseChip.locator('.ob-sc-n')).toHaveText('1');

    // The merged row must render as chase-in-flight and show OrderCard's
    // `chase:#N` marker sourced from the ALGO row's attempts field, even
    // though the row that actually won the merge is the BROKER row.
    await chaseChip.click();
    const card = page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-LIVECHASE' });
    await expect(card).toHaveCount(1);
    await expect(card.locator('.log-chip', { hasText: 'chase:' })).toHaveText(/chase:#4/);
  });

  test('Open chip alone (no Chase click) still shows the live-chased row only once', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockOrdersEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    // Exactly one order card overall (broker + algo rows dedup to one row,
    // the broker-origin one) — if the dedup key itself regressed, this
    // count would be 2, a separate pre-existing defect class this spec
    // also incidentally guards.
    const openChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Open$/ }) });
    await expect(openChip.locator('.ob-sc-n')).toHaveText('1');
  });
});

test.describe('OrderBook — Cancel stale-broker-id guard (plan item 3)', () => {
  test.setTimeout(60_000);

  test('Cancel refuses and toasts when chase has superseded the broker_order_id since the last poll', async ({ page }) => {
    await authOnce(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));

    // Call-counted /algo/recent mock — call #1 (initial _loadOrders on
    // mount) returns the row still pointing at the ORIGINAL broker id
    // (B9001, matching the broker book below) so the Cancel button
    // renders normally. Call #2+ (the guard's fresh re-fetch, triggered
    // by the Cancel click itself — not a routine poll tick, since the
    // click happens well inside the 5s poll interval) returns the SAME
    // algo row id now pointing at a NEW broker_order_id (B9002) — the
    // exact shape a real cancel-and-replace produces between polls.
    let _algoCallCount = 0;
    let _deleteCalled = false;
    await page.route('**/api/orders/**', async (route) => {
      const req = route.request();
      const url = req.url();
      if (req.method() === 'DELETE') {
        _deleteCalled = true;
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
        return;
      }
      if (req.method() !== 'GET') { await route.continue(); return; }
      if (url.includes('/orders/gtts')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ gtts: [], count: 0 }) });
        return;
      }
      if (url.includes('/orders/algo/recent')) {
        _algoCallCount += 1;
        const bid = _algoCallCount === 1 ? _LIVE_BROKER_ORDER_ID : 'B9002';
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify([{ ..._ALGO_ROWS[0], broker_order_id: bid }]),
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

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    // Switch to the Open chip (Cancel only renders for OPEN broker rows
    // via `_isOpenBroker`) and click Cancel on the live-chased card.
    const openChip = page.locator('.ob-status-bar .ob-sc', { has: page.locator('.ob-sc-l', { hasText: /^Open$/ }) });
    await openChip.click();
    const card = page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-LIVECHASE' });
    await expect(card).toHaveCount(1);
    await card.locator('.lp-oc-cancel').click();

    // The guard's fresh re-fetch sees the superseded broker_order_id and
    // refuses — no DELETE ever goes out, and a toast informs the operator.
    await expect(page.locator('.rbq-toast-msg', { hasText: /superseded/i })).toBeVisible({ timeout: 5_000 });
    expect(_deleteCalled).toBe(false);
    expect(_algoCallCount).toBeGreaterThanOrEqual(2);
  });
});
