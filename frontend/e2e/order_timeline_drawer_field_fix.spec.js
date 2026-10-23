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
 * 2026-10-23 AMENDMENT — the navbar chip's click target changed from this
 * OrderTimelineDrawer event-log to a live-STATUS view (ChaseCard — see
 * chase_badge_live_status_drawer.spec.js for that surface's own coverage).
 * `openOrderIds`/`chaseOrderContext` (this spec's original subject) are
 * KEPT but now feed only the chip's tooltip + 60s post-terminal linger —
 * the chip's displayed NUMBER and the drawer's actual content both come
 * from ChaseCard's own `/orders/chases/active` poll instead. The test
 * below is updated to mock that endpoint too and assert the new
 * ChaseCard-format drawer content, while still proving the original
 * fields-mismatch fix: real symbol/side/qty/mode still resolve from
 * `orderContext` (sourced from `/orders/algo/recent`) rather than from
 * nonexistent event fields — just surfaced via the chip's tooltip
 * (`openSymbols`) and `_ensureChaseOrderContext` now, not via the old
 * drawer's per-order header row.
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

// ── Real AlgoOrderInfo shape, shaped for ChaseCard's /chases/active poll
//    (the new source for the chip's displayed number + drawer content).
//    Orders 101/102 are active (OPEN, non-terminal); 103 is COMPLETE
//    (terminal — chase.py / list_active_chases never returns a terminal
//    row from this endpoint), so the active count is 2, not 3. ───────────
const _NOW_SEC = Math.floor(Date.now() / 1000);
const _CHASES_ACTIVE = [
  { id: 101, account: 'T1', transaction_type: 'BUY', quantity: 50,
    symbol: 'RBQ-LIVE1', current_limit: 101, initial_price: 100,
    attempts: 2, status: 'active', next_attempt_at: _NOW_SEC + 10,
    mode: 'live', child_order_ids: [] },
  { id: 102, account: 'T1', transaction_type: 'SELL', quantity: 20,
    symbol: 'RBQ-LIVE2', current_limit: 205, initial_price: 200,
    attempts: 3, status: 'active', next_attempt_at: _NOW_SEC + 5,
    mode: 'live', child_order_ids: [] },
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
    if (url.includes('/orders/chases/active')) {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify(_CHASES_ACTIVE),
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

  test('navbar chase chip appears; the field-mismatch fix still resolves real symbol/side/qty/mode via orderContext (now surfaced through the tooltip + ChaseCard live view, not the old event-log)', async ({ page }) => {
    await loginAsAdmin(page);
    await mockOrdersEndpoints(page);

    // Run on /pulse, not /orders — /orders embeds its OWN separate
    // <ChaseCard> instance, which would make a bare `.cc-root` locator
    // ambiguous (two instances: the page's own card + the header drawer).
    await page.goto('/pulse');
    await page.waitForLoadState('domcontentloaded');

    // 1. The chip is reachable at all — previously structurally impossible
    //    (showChaseChip was always false under the old field-mismatch bug).
    const chip = page.locator('.chase-chip:visible');
    await expect(chip).toBeVisible({ timeout: 15_000 });
    // 2026-10-23: the chip's NUMBER now reflects ChaseCard's own
    // `/orders/chases/active` poll (2 active rows), not the 3 distinct
    // order_ids in the `/orders/events/recent` feed — this is the fix's
    // whole point: the two could previously disagree.
    await expect(chip).toContainText('2');
    // The field-mismatch fix's original subject — real per-order
    // symbol/side/qty/mode resolved via orderContext from
    // `/orders/algo/recent`, never invented event fields — is still
    // exercised: the tooltip lists symbols resolved through
    // `chaseOrderContext`, built from the same `_ALGO_ROWS` fixture.
    await expect(chip).toHaveAttribute('title', /RBQ-LIVE1|RBQ-LIVE2|RBQ-PAPER1/);

    // The chip carries a perpetual `algo-mode-dot` pulse animation (CLAUDE.md
    // palette convention, unrelated to this fix) — Playwright's actionability
    // check never sees it "stable" across animation frames, so force the click.
    await chip.click({ force: true });

    // The live ChaseCard view, not the old event-log drawer.
    const ccRoot = page.locator('.chd-drawer .cc-root');
    await expect(ccRoot).toBeVisible({ timeout: 10_000 });
    const rows = page.locator('.chd-drawer .cc-row:not(.cc-row-h)');
    await expect(rows).toHaveCount(2);

    // Real symbol/side/qty/mode — straight from `_CHASES_ACTIVE`, not from
    // any invented event field.
    const liveRow = page.locator('.chd-drawer .cc-row', { hasText: 'RBQ-LIVE1' });
    await expect(liveRow.locator('.cc-side-buy')).toHaveText('BUY');
    await expect(liveRow.locator('.cc-col-qty')).toHaveText('50');
    await expect(liveRow).toContainText('LIVE');

    const sellRow = page.locator('.chd-drawer .cc-row', { hasText: 'RBQ-LIVE2' });
    await expect(sellRow.locator('.cc-side-sell')).toHaveText('SELL');
    await expect(sellRow).toContainText('LIVE');

    // The old event-log drawer never renders for this entry point.
    await expect(page.locator('.otd-drawer')).toHaveCount(0);
  });
});
