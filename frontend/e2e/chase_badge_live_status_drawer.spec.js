/**
 * chase_badge_live_status_drawer.spec.js
 *
 * 2026-10-23 — the header `.chase-chip` badge's click target changed from
 * an event-TIMELINE log (OrderTimelineDrawer, `.otd-drawer`/`.otd-section`)
 * to a live-STATUS view (ChaseCard — current price, countdown to next
 * re-quote, Kill button). See (algo)/+layout.svelte's own comment above
 * the drawer markup for the full mount/visibility design.
 *
 * This does NOT change OrderBook.svelte's own per-order "view timeline"
 * entry point — clicking an OrderCard body still opens a LOCAL
 * OrderTimelineDrawer instance scoped to that one order (covered by
 * order_timeline_per_order_view.spec.js). Test 3 below is a regression
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

/** Mocks the orders endpoints the header chase chip + ChaseCard touch.
 *  `chases` feeds `/orders/chases/active` (ChaseCard's own poll — drives
 *  `activeCount`, the chip's displayed number + visibility). `events`
 *  feeds `/orders/events/recent` (the layout's own `pollChase()` — drives
 *  only the tooltip + 60s post-terminal linger now, not the number). */
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

const _ACTIVE_CHASE = {
  id: 501, account: 'T1', transaction_type: 'BUY', quantity: 50,
  symbol: 'RBQ-CHASEBADGE', current_limit: 102, initial_price: 100,
  attempts: 2, status: 'active', next_attempt_at: _NOW_SEC + 12,
  mode: 'live', child_order_ids: [],
};

test.describe('Header chase badge — live ChaseCard view (Fix 3)', () => {
  test.setTimeout(60_000);

  test('mocked active chase: clicking the badge shows the ChaseCard live view (price, countdown, Kill)', async ({ page }) => {
    await loginAsAdmin(page);
    await mockChaseEndpoints(page, { chases: [_ACTIVE_CHASE] });

    await page.goto('/pulse');
    await page.waitForLoadState('domcontentloaded');

    const chip = page.locator('.chase-chip:visible').first();
    await expect(chip).toBeVisible({ timeout: 15_000 });
    await expect(chip).toContainText('1');

    await chip.click({ force: true });

    // The ChaseCard-format live view, not the old event-log drawer.
    const ccRoot = page.locator('.chd-drawer .cc-root');
    await expect(ccRoot).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.chd-drawer .cc-row', { hasText: 'RBQ-CHASEBADGE' })).toBeVisible();
    // Price.
    await expect(page.locator('.chd-drawer .cc-col-limit', { hasText: '₹' })).toBeVisible();
    // Countdown.
    await expect(page.locator('.chd-drawer .cc-countdown')).toBeVisible();
    // Kill button.
    await expect(page.locator('.chd-drawer .cc-kill', { hasText: 'Kill' })).toBeVisible();

    // Never the old event-timeline drawer for this entry point.
    await expect(page.locator('.otd-drawer')).toHaveCount(0);
  });

  test('zero active chases: clicking the badge (via a lingering terminal event) shows an explicit "no active chases" message, not a blank drawer', async ({ page }) => {
    await loginAsAdmin(page);
    // No active chase rows at all — but a just-finished terminal event
    // (within the 60s linger window) keeps the chip visible so there is
    // something to click.
    const recentTerminal = [
      { id: 1, order_id: 777, ts: new Date().toISOString(), kind: 'fill', message: 'x', payload_json: null },
    ];
    await mockChaseEndpoints(page, { chases: [], events: recentTerminal });

    await page.goto('/pulse');
    await page.waitForLoadState('domcontentloaded');

    const chip = page.locator('.chase-chip:visible').first();
    await expect(chip).toBeVisible({ timeout: 15_000 });
    await expect(chip).toContainText('0');

    await chip.click({ force: true });

    const drawer = page.locator('.chd-drawer.chd-drawer-open');
    await expect(drawer).toBeVisible({ timeout: 10_000 });
    await expect(drawer.locator('.chd-empty', { hasText: 'No active chases' })).toBeVisible();
    // ChaseCard itself renders nothing when it has zero chase/pending/draft
    // rows — the shell (not ChaseCard) must be the one showing the message.
    await expect(drawer.locator('.cc-root')).toHaveCount(0);
  });

  test('a failed /chases/active fetch surfaces a short error banner in the drawer shell (not a silent "no active chases")', async ({ page }) => {
    await loginAsAdmin(page);
    // A fresh terminal event keeps the chip visible (via the 60s linger)
    // despite the chases feed itself failing, so there's something to
    // click. `_chaseCardActiveCount` stays 0 on a failed fetch (ChaseCard
    // never populates `_chases` from a thrown error), so without the
    // `loadError` bindable the shell would show "No active chases" — a
    // confirmed-empty message — for what is actually an unknown/failed
    // state.
    const recentTerminal = [
      { id: 1, order_id: 778, ts: new Date().toISOString(), kind: 'fill', message: 'x', payload_json: null },
    ];
    await mockChaseEndpoints(page, { chasesError: true, events: recentTerminal });

    await page.goto('/pulse');
    await page.waitForLoadState('domcontentloaded');

    const chip = page.locator('.chase-chip:visible').first();
    await expect(chip).toBeVisible({ timeout: 15_000 });

    await chip.click({ force: true });

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
  });
});
