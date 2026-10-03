/**
 * order_timeline_drawer_field_fix.spec.js
 *
 * Covers the Sprint 2a field-mismatch fix
 * (docs/proposals/SPRINT2_LAYER_INTEGRATION.md §1 finding 1, §4.2):
 *
 *  - OrderTimelineDrawer.svelte previously read invented fields
 *    (ev.symbol/ev.side/ev.qty/ev.mode/ev.created_at/ev.price) that don't
 *    exist on the real `GET /api/orders/events/recent` response shape
 *    (`id, order_id, ts, kind, message, payload_json`).
 *  - (algo)/+layout.svelte's own feed (`openOrderIds` / `lastTerminalAt`)
 *    had the identical bug — it compared a nonexistent `ev.status`/
 *    `ev.kind === 'open'` and read `ev.created_at`/`ev.timestamp`, so
 *    `showChaseChip` was ALWAYS false and the chip + drawer were
 *    structurally unreachable regardless of the drawer's own fix.
 *
 * This spec mocks the real response shapes for both
 * `/api/orders/events/recent` and `/api/orders/algo/recent` (the latter
 * supplies the per-order symbol/side/qty/mode context events don't carry)
 * and asserts:
 *   1. The navbar chase chip becomes visible (proves the layout-level feed
 *      fix — previously impossible under any mock).
 *   2. Opening the drawer shows the real symbol/side/qty/mode per order,
 *      sourced from orderContext, not from the (nonexistent) event fields.
 *   3. A LIVE order never renders the PAPER pill (the old `?? 'paper'`
 *      default bug).
 *   4. Price is parsed out of `payload_json`, matching the real shapes
 *      written by chase.py / paper.py / orders_postback.py.
 *   5. Sections sort non-terminal-first, newest-activity-first within
 *      group — using the real event `ts`, not a nonexistent `created_at`.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/order_timeline_drawer_field_fix.spec.js --project=chromium-desktop
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

// ── Real AlgoOrderEventInfo shape (id, order_id, ts, kind, message,
//    payload_json) — backend/api/routes/orders_helpers.py:591-598. ─────────
const _EVENTS = [
  // Order 101 — LIVE, non-terminal, latest activity at 09:05.
  { id: 1, order_id: 101, ts: '2026-01-01T09:00:00Z', kind: 'placed',
    message: 'live ticket BUY 50 RBQ-LIVE1', payload_json: null },
  { id: 2, order_id: 101, ts: '2026-01-01T09:05:00Z', kind: 'chase_modify',
    message: 'Chase attempt 2: BUY 50 @ 101',
    payload_json: JSON.stringify({ price: 101, attempt: 2 }) },
  // Order 102 — LIVE, non-terminal, latest activity at 09:20 (more recent
  // than order 101 — must render FIRST).
  { id: 3, order_id: 102, ts: '2026-01-01T09:10:00Z', kind: 'placed',
    message: 'live ticket SELL 20 RBQ-LIVE2', payload_json: null },
  { id: 4, order_id: 102, ts: '2026-01-01T09:20:00Z', kind: 'chase_modify',
    message: 'Chase attempt 3: SELL 20 @ 205',
    payload_json: JSON.stringify({ price: 205, attempt: 3 }) },
  // Order 103 — PAPER, terminal (fill) — must render LAST regardless of
  // activity recency.
  { id: 5, order_id: 103, ts: '2026-01-01T08:00:00Z', kind: 'placed',
    message: '[PAPER] manual BUY 5 RBQ-PAPER1', payload_json: null },
  { id: 6, order_id: 103, ts: '2026-01-01T08:10:00Z', kind: 'fill',
    message: 'FILLED @₹303.25 after 1 chase(s)',
    payload_json: JSON.stringify({ fill_price: 303.25, attempts: 1 }) },
];

// ── Real AlgoOrderInfo shape (subset) — supplies the per-order context
//    (symbol/side/qty/mode) events themselves don't carry. ─────────────────
const _ALGO_ROWS = [
  { id: 101, account: 'T1', symbol: 'RBQ-LIVE1', exchange: 'NFO', transaction_type: 'BUY',
    quantity: 50, initial_price: 100, current_limit: 101, fill_price: null,
    attempts: 2, status: 'OPEN', engine: 'chase', mode: 'live', detail: null,
    created_at: '2026-01-01T09:00:00' },
  { id: 102, account: 'T1', symbol: 'RBQ-LIVE2', exchange: 'NFO', transaction_type: 'SELL',
    quantity: 20, initial_price: 200, current_limit: 205, fill_price: null,
    attempts: 3, status: 'OPEN', engine: 'chase', mode: 'live', detail: null,
    created_at: '2026-01-01T09:10:00' },
  { id: 103, account: 'T1', symbol: 'RBQ-PAPER1', exchange: 'NFO', transaction_type: 'BUY',
    quantity: 5, initial_price: 300, current_limit: 300, fill_price: 303.25,
    attempts: 1, status: 'COMPLETE', engine: 'chase', mode: 'paper', detail: null,
    created_at: '2026-01-01T08:00:00' },
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
    if (url.includes('/orders/events/recent')) {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify(_EVENTS),
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
        body: JSON.stringify({ rows: [], refreshed_at: new Date().toISOString() }),
      });
      return;
    }
    await route.continue();
  });
}

test.describe('OrderTimelineDrawer — field-mismatch fix', () => {
  test.setTimeout(60_000);

  test('navbar chase chip appears and drawer shows real fields from orderContext, not invented event fields', async ({ page }) => {
    await loginAsAdmin(page);
    await mockOrdersEndpoints(page);

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');

    // 1. The chip is reachable at all — previously structurally impossible
    //    (showChaseChip was always false under the old field-mismatch bug).
    const chip = page.locator('.chase-chip:visible');
    await expect(chip).toBeVisible({ timeout: 15_000 });
    // 3 distinct order_ids in _EVENTS are all OPEN per the mocked backend
    // contract (status=open query) — chip shows the open-order count.
    await expect(chip).toContainText('3');

    // The chip carries a perpetual `algo-mode-dot` pulse animation (CLAUDE.md
    // palette convention, unrelated to this fix) — Playwright's actionability
    // check never sees it "stable" across animation frames, so force the click.
    await chip.click({ force: true });

    const drawer = page.locator('.otd-drawer');
    await expect(drawer).toBeVisible();

    const sections = drawer.locator('.otd-section');
    await expect(sections).toHaveCount(3);

    // 5. Sort order: order 102 (latest activity 09:20) first, order 101
    //    (latest activity 09:05) second, order 103 (terminal) last —
    //    regardless of its own 08:10 timestamp being earliest of all.
    await expect(sections.nth(0)).toContainText('RBQ-LIVE2');
    await expect(sections.nth(1)).toContainText('RBQ-LIVE1');
    await expect(sections.nth(2)).toContainText('RBQ-PAPER1');
    await expect(sections.nth(2)).toHaveClass(/otd-section-terminal/);

    // 2 + 3. Real symbol/side/qty/mode come from orderContext — a LIVE
    //    order never falls back to the PAPER pill.
    const liveSection = sections.filter({ hasText: 'RBQ-LIVE1' });
    await expect(liveSection.locator('.otd-side')).toHaveText('BUY');
    await expect(liveSection.locator('.otd-qty')).toHaveText('50');
    await expect(liveSection.locator('.otd-mode-pill')).toHaveText('LIVE');
    await expect(liveSection.locator('.otd-mode-pill')).not.toHaveText('PAPER');

    const sellSection = sections.filter({ hasText: 'RBQ-LIVE2' });
    await expect(sellSection.locator('.otd-side')).toHaveText('SELL');
    await expect(sellSection.locator('.otd-mode-pill')).toHaveText('LIVE');

    const paperSection = sections.filter({ hasText: 'RBQ-PAPER1' });
    await expect(paperSection.locator('.otd-mode-pill')).toHaveText('PAPER');

    // 4. Price is parsed from payload_json (chase_modify → payload.price;
    //    fill → payload.fill_price) — never read from a nonexistent
    //    ev.price/ev.limit_price on the raw event row.
    await expect(liveSection.locator('.otd-ev-price', { hasText: '₹101.00' })).toBeVisible();
    await expect(sellSection.locator('.otd-ev-price', { hasText: '₹205.00' })).toBeVisible();
    await expect(paperSection.locator('.otd-ev-price', { hasText: '₹303.25' })).toBeVisible();

    // "placed" events here carry no payload at all (live ticket convention)
    // — must render with no price chip rather than crashing/showing ₹NaN.
    await expect(liveSection.locator('.otd-ev-kind', { hasText: 'placed' })).toBeVisible();
  });
});
