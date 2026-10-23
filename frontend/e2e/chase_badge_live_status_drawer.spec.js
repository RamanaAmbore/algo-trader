/**
 * chase_badge_live_status_drawer.spec.js
 *
 * 2026-10-23 — the header `.chase-chip` badge's click target changed from
 * an event-TIMELINE log (OrderTimelineDrawer, `.otd-drawer`/`.otd-section`)
 * to a live-STATUS view (ChaseCard — current price, countdown to next
 * re-quote, Kill button). See (algo)/+layout.svelte's own comment above
 * the drawer markup for the full mount/visibility design.
 *
 * 2026-10-23 (relocation) — the standalone `.chase-chip` header element
 * that used to open this drawer is REMOVED entirely. The trigger moved to
 * a long-press (mobile) / right-click (desktop) gesture on the per-page
 * Order button (`.pha-order`, PageHeaderActions.svelte) via the shared
 * `chaseActiveCount`/`chaseDrawerOpen` stores ($lib/stores.js). This file
 * keeps its original rich ChaseCard-content assertions (price, countdown,
 * pulse dot, limit-moved chip, Kill button, empty-state message, error
 * banner, and the OrderBook-per-order-timeline regression guard) — only
 * the trigger mechanism changed. Coverage for the indicator dot, the
 * one-time discoverability hint, and "plain click still opens the order
 * ticket unaffected" lives in order_header_chase_gesture.spec.js.
 *
 * This does NOT change OrderBook.svelte's own per-order "view timeline"
 * entry point — clicking an OrderCard body still opens a LOCAL
 * OrderTimelineDrawer instance scoped to that one order (covered by
 * order_timeline_per_order_view.spec.js). Test 4 below is a regression
 * guard confirming that path is untouched.
 *
 * Run on a page that does NOT itself embed a separate `<ChaseCard>`
 * instance (/orders does; /pulse does not) so there is never more than
 * one `.cc-root` in the DOM to disambiguate.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/chase_badge_live_status_drawer.spec.js --project=chromium-desktop
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

/** Mocks the orders endpoints the Order-button gesture + ChaseCard touch.
 *  `chases` feeds `/orders/chases/active` (ChaseCard's own poll — drives
 *  `activeCount`, the Order button's indicator dot + the drawer's empty/
 *  error state). `events` feeds `/orders/events/recent` (the layout's own
 *  `pollChase()` — drives only the adaptive poll cadence now, no UI). */
async function mockChaseEndpoints(page, { chases = [], events = [], algoRows = [], chasesError = false } = {}) {
  await page.route('**/api/orders/**', async (route) => {
    const req = route.request();
    const url = req.url();
    if (req.method() !== 'GET') { await route.continue(); return; }
    if (url.includes('/orders/chases/active')) {
      if (chasesError) {
        await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'boom' }) });
        return;
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(chases) });
      return;
    }
    if (url.includes('/orders/events/recent')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(events) });
      return;
    }
    if (url.includes('/orders/algo/recent')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(algoRows) });
      return;
    }
    if (url.includes('/orders/gtts')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ gtts: [], count: 0 }) });
      return;
    }
    if (/\/api\/orders\/?(\?.*)?$/.test(url)) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows: [], refreshed_at: new Date().toISOString() }) });
      return;
    }
    await route.continue();
  });
}

const _NOW_SEC = Math.floor(Date.now() / 1000);

// Real backend vocabulary — `backend/api/routes/orders.py:list_active_chases`
// filters `AlgoOrder.status.in_(["OPEN", "CANCEL_FAILED"])`; it never
// returns 'active'. A fixture mocking 'active' here masked the countdown/
// pulse-dot gate bug in ChaseCard.svelte (fixed 2026-10-23) — this fixture
// must use real status vocabulary so a regression in that gate shows up
// here again.
const _ACTIVE_CHASE = {
  id: 501, account: 'T1', transaction_type: 'BUY', quantity: 50,
  symbol: 'RBQ-CHASEBADGE', current_limit: 102, initial_price: 100,
  attempts: 2, status: 'OPEN', next_attempt_at: _NOW_SEC + 12,
  mode: 'live', child_order_ids: [],
};

test.describe('Order-button chase gesture — live ChaseCard view', () => {
  test.setTimeout(60_000);

  test('mocked active chase (real OPEN status): right-clicking the Order button shows the ChaseCard live view (price, countdown, pulse dot, Kill)', async ({ page }) => {
    await loginAsAdmin(page);
    await mockChaseEndpoints(page, { chases: [_ACTIVE_CHASE] });

    await page.goto('/pulse');
    await page.waitForLoadState('domcontentloaded');

    const orderBtn = page.locator('.pha-order:visible').first();
    await expect(orderBtn).toBeVisible({ timeout: 15_000 });
    // Indicator dot visible for an active chase.
    await expect(orderBtn.locator('.pha-chase-dot')).toBeVisible({ timeout: 10_000 });

    await orderBtn.click({ button: 'right' });

    // The ChaseCard-format live view, not the old event-log drawer.
    const ccRoot = page.locator('.chd-drawer .cc-root');
    await expect(ccRoot).toBeVisible({ timeout: 10_000 });
    const row = page.locator('.chd-drawer .cc-row', { hasText: 'RBQ-CHASEBADGE' });
    await expect(row).toBeVisible();
    // Price.
    await expect(row.locator('.cc-col-limit')).toContainText('₹');
    // 2026-10-23 regression guard — the countdown and pulse dot are gated
    // on `row.status === 'OPEN'` (the real backend vocabulary; the fixture
    // above now mocks 'OPEN', not the invented 'active' value that used to
    // mask this exact bug in production). Assert the countdown renders a
    // real ticking numeric value, not just that the element exists.
    const countdown = row.locator('.cc-countdown');
    await expect(countdown).toBeVisible();
    await expect(countdown).toHaveText(/^(\d+s|now)$/);
    await expect(row.locator('.cc-pulse')).toBeVisible();
    // Limit-moved chip — current_limit (102) vs initial_price (100) have
    // diverged after attempts=2 (1 initial placement + 1 re-quote), so the
    // chip reflects 1 modify, not the raw placement count of 2.
    await expect(row.locator('.cc-limit-moved')).toHaveText('·1');
    // Kill button.
    await expect(page.locator('.chd-drawer .cc-kill', { hasText: 'Kill' })).toBeVisible();

    // Never the old event-timeline drawer for this entry point.
    await expect(page.locator('.otd-drawer')).toHaveCount(0);
    // The old standalone chip no longer exists anywhere.
    await expect(page.locator('.chase-chip')).toHaveCount(0);
  });

  test('zero active chases: right-clicking the Order button (no indicator dot) still shows an explicit "no active chases" message, not a blank drawer', async ({ page }) => {
    await loginAsAdmin(page);
    // The Order-button gesture no longer depends on a lingering terminal
    // event to stay "reachable" — the button itself is always present
    // whenever the page doesn't pass hideOrder, regardless of chase
    // count. Confirm the gesture opens the drawer even with zero actives
    // and no indicator dot showing.
    await mockChaseEndpoints(page, { chases: [], events: [] });

    await page.goto('/pulse');
    await page.waitForLoadState('domcontentloaded');

    const orderBtn = page.locator('.pha-order:visible').first();
    await expect(orderBtn).toBeVisible({ timeout: 15_000 });
    await expect(orderBtn.locator('.pha-chase-dot')).toHaveCount(0);

    await orderBtn.click({ button: 'right' });

    const drawer = page.locator('.chd-drawer.chd-drawer-open');
    await expect(drawer).toBeVisible({ timeout: 10_000 });
    await expect(drawer.locator('.chd-empty', { hasText: 'No active chases' })).toBeVisible();
    // ChaseCard itself renders nothing when it has zero chase/pending/draft
    // rows — the shell (not ChaseCard) must be the one showing the message.
    await expect(drawer.locator('.cc-root')).toHaveCount(0);
  });

  test('a failed /chases/active fetch surfaces a short error banner in the drawer shell (not a silent "no active chases")', async ({ page }) => {
    await loginAsAdmin(page);
    await mockChaseEndpoints(page, { chasesError: true, events: [] });

    await page.goto('/pulse');
    await page.waitForLoadState('domcontentloaded');

    const orderBtn = page.locator('.pha-order:visible').first();
    await expect(orderBtn).toBeVisible({ timeout: 15_000 });

    await orderBtn.click({ button: 'right' });

    const drawer = page.locator('.chd-drawer.chd-drawer-open');
    await expect(drawer).toBeVisible({ timeout: 10_000 });
    await expect(drawer.locator('.chd-error')).toBeVisible({ timeout: 10_000 });
    await expect(drawer.locator('.chd-empty')).toHaveCount(0);
    await expect(drawer.locator('.cc-root')).toHaveCount(0);
  });

  test('regression guard: OrderBook\'s own per-order "view timeline" still opens the event-log drawer, unaffected', async ({ page }) => {
    const brokerRow = {
      order_id: '201', tradingsymbol: 'NIFTY26JUN25000CE', exchange: 'NFO',
      transaction_type: 'BUY', quantity: 50, status: 'OPEN',
      order_timestamp: '2026-01-01 09:00:00', price: 100, account: 'T1',
      variety: 'regular', product: 'MIS',
    };
    await loginAsAdmin(page);
    await page.route('**/api/orders/**', async (route) => {
      const req = route.request();
      const url = req.url();
      if (req.method() !== 'GET') { await route.continue(); return; }
      const perOrderMatch = url.match(/\/orders\/(\d+)\/events(?:\?|$)/);
      if (perOrderMatch) {
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify([
            { id: 1, order_id: 201, ts: '2026-01-01T09:00:00Z', kind: 'placed', message: 'x', payload_json: null },
          ]),
        });
        return;
      }
      if (url.includes('/orders/chases/active')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
        return;
      }
      if (url.includes('/orders/events/recent')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
        return;
      }
      if (url.includes('/orders/algo/recent')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
        return;
      }
      if (url.includes('/orders/gtts')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ gtts: [], count: 0 }) });
        return;
      }
      if (/\/api\/orders\/?(\?.*)?$/.test(url)) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows: [brokerRow], refreshed_at: new Date().toISOString() }) });
        return;
      }
      await route.continue();
    });

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');

    const card = page.locator('.order-card').filter({ hasText: /NIFTY.*25000.*CE/ });
    await expect(card).toBeVisible({ timeout: 15_000 });
    await card.click();

    const drawer = page.locator('.otd-drawer');
    await expect(drawer).toBeVisible();
    await expect(drawer.locator('.otd-symbol')).toHaveText(/NIFTY.*25000.*CE/);
    // The header's own live-status drawer never opened as a side effect.
    await expect(page.locator('.chd-drawer.chd-drawer-open')).toHaveCount(0);
    // The old standalone chip no longer exists anywhere, including on
    // the /orders page (which hides the Order button entirely).
    await expect(page.locator('.chase-chip')).toHaveCount(0);
  });
});
