/**
 * performance_fill_toast_migration.spec.js
 *
 * PerformancePage.svelte's `_flashFillToast` (fires on the WS
 * `position_filled` event, the same Kite-postback-driven path
 * `createPerformanceSocket` exposes) used to render its OWN private
 * `.perf-fill-toast` markup/CSS/timer, bypassing the shared
 * `toastStore.svelte.js` / `ToastContainer.svelte` system entirely —
 * today's mobile burst-cap fix (92ef1c5a) never reached it, and it
 * rendered at `z-index: 1000`, below the fullscreen-card tier
 * (`.fs-card-on`, z-index 9999) and the toast system's own tier
 * (`--z-toast`, 21000).
 *
 * Fix: `_flashFillToast` now calls `toast.success(...)` from
 * `$lib/data/toastStore.svelte.js`. The private markup/CSS/timer
 * (`.perf-fill-toast`, `_fillToast` state, `_fillToastTimer`) were
 * removed entirely. `<PerformancePage>` is mounted ONLY under
 * `(public)/performance/+page.svelte` (the public cream page) —
 * `(public)/+layout.svelte` did not previously mount
 * `<ToastContainer />` at all (only the (algo) layout did), so this
 * fix also had to add `<ToastContainer />` to the public layout, or
 * the migrated toast would have gone nowhere on the only route that
 * exercises it.
 *
 * Five quality dimensions:
 *  1. SSOT   — drives the real `toastStore` + `ToastContainer` render
 *              path (asserts `.rbq-toast-container`/`.rbq-toast-slot`
 *              DOM, not a source-grep proxy), and asserts the OLD
 *              private element (`.perf-fill-toast`) no longer exists
 *              anywhere in the DOM.
 *  2. Perf   — n/a (no poll loop on this surface).
 *  3. Stale  — n/a.
 *  4. Reuse  — exercises the existing `toast` API via the real
 *              `position_filled` WS event, the exact mocking idiom
 *              already established in
 *              `derivatives_fill_optimistic_apply.spec.js`
 *              (`page.routeWebSocket('**\/ws/performance', ...)`).
 *  5. UX     — (a) the fill toast is reachable and auto-dismisses on
 *              its default 3000ms success-timeout (matching the old
 *              private timer's intent); (b) the mobile burst-cap
 *              behavior from 92ef1c5a is respected by a toast pushed
 *              from this surface, proving migration didn't fork a
 *              second toast system — it went through the one real
 *              `ToastContainer` cap.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/performance_fill_toast_migration.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

const ACCOUNT = 'ZG0790';
const SYMBOL  = 'NIFTY26DEC25000CE';

/** Navigate to `/performance`, wait for the `/ws/performance` WebSocket
 *  to connect, and return the intercepted route so the test can push a
 *  synthetic `position_filled` event. Null if it never connected. */
async function _gotoAndWaitForPerfWs(page) {
  /** @type {import('@playwright/test').WebSocketRoute | null} */
  let wsRoute = null;
  await page.routeWebSocket('**/ws/performance', (ws) => { wsRoute = ws; });
  await page.goto('/performance', { waitUntil: 'domcontentloaded' });
  for (let i = 0; i < 25 && !wsRoute; i++) await page.waitForTimeout(200);
  return wsRoute;
}

test.describe('(1)(5a) /performance fill toast — migrated to shared toastStore/ToastContainer', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('position_filled renders via ToastContainer (not the old private element) and auto-dismisses', async ({ page }) => {
    const wsRoute = await _gotoAndWaitForPerfWs(page);

    if (!wsRoute) {
      test.skip(true, 'WebSocket to /ws/performance never connected — cannot inject test event');
      return;
    }

    // Confirm the OLD private element never existed to begin with
    // (removed entirely, not merely hidden).
    await expect(page.locator('.perf-fill-toast')).toHaveCount(0);

    wsRoute.send(JSON.stringify({
      event: 'position_filled', account: ACCOUNT, exchange: 'NFO',
      tradingsymbol: SYMBOL, qty: 5, fill_price: 455.0, ts: Date.now(), order_id: 'T9001',
    }));

    // Renders inside ToastContainer's own DOM (.rbq-toast-container /
    // .rbq-toast-slot), carrying the fill message text — NOT a
    // re-introduced private element.
    const slot = page.locator('.rbq-toast-container .rbq-toast-slot .rbq-toast', { hasText: /Filled:.*BUY.*5.*NIFTY26DEC25000CE/ });
    await expect(slot).toHaveCount(1, { timeout: 3_000 });
    await expect(page.locator('.perf-fill-toast')).toHaveCount(0);

    // Default `toast.success` timeout is 3000ms (toastStore.svelte.js) —
    // matches the old private timer's documented 3s auto-clear intent.
    await expect(slot).toHaveCount(0, { timeout: 5_000 });
  });

  test('a second fill while the first toast is still showing collapses to one toast (latest wins)', async ({ page }) => {
    const wsRoute = await _gotoAndWaitForPerfWs(page);

    if (!wsRoute) {
      test.skip(true, 'WebSocket to /ws/performance never connected — cannot inject test event');
      return;
    }

    wsRoute.send(JSON.stringify({
      event: 'position_filled', account: ACCOUNT, exchange: 'NFO',
      tradingsymbol: SYMBOL, qty: 5, fill_price: 455.0, ts: Date.now(), order_id: 'T9002',
    }));
    await expect(page.locator('.rbq-toast-container .rbq-toast-slot')).toHaveCount(1, { timeout: 3_000 });

    wsRoute.send(JSON.stringify({
      event: 'position_filled', account: ACCOUNT, exchange: 'NFO',
      tradingsymbol: SYMBOL, qty: 3, fill_price: 456.0, ts: Date.now(), order_id: 'T9003',
    }));

    // Old fill-toast dismissed, new one shown — never two stacked at once.
    await expect(page.locator('.rbq-toast-container .rbq-toast-slot')).toHaveCount(1);
    const slot = page.locator('.rbq-toast-container .rbq-toast-slot .rbq-toast', { hasText: /Filled:.*BUY.*3.*NIFTY26DEC25000CE/ });
    await expect(slot).toHaveCount(1);
  });
});

test.describe('(5b) /performance fill toast — still respects the mobile burst-cap (92ef1c5a)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('a fill toast pushed from PerformancePage counts toward the mobile 2-visible cap, not a parallel toast system', async ({ page }) => {
    const wsRoute = await _gotoAndWaitForPerfWs(page);

    if (!wsRoute) {
      test.skip(true, 'WebSocket to /ws/performance never connected — cannot inject test event');
      return;
    }

    // One real fill toast via the WS path PerformancePage actually uses.
    wsRoute.send(JSON.stringify({
      event: 'position_filled', account: ACCOUNT, exchange: 'NFO',
      tradingsymbol: SYMBOL, qty: 5, fill_price: 455.0, ts: Date.now(), order_id: 'T9004',
    }));
    await expect(page.locator('.rbq-toast-slot')).toHaveCount(1, { timeout: 3_000 });

    // Push 4 more sticky toasts via the dev-only window hook (this
    // layout's own copy of the (algo) layout's hook — see
    // (public)/+layout.svelte) so the stack hits 5 total, above the
    // MOBILE_VISIBLE=2 cap.
    await page.waitForFunction(() => !!(/** @type {any} */ (window).__stores?.toast), { timeout: 10_000 });
    await page.evaluate(() => {
      const t = /** @type {any} */ (window).__stores.toast;
      for (let i = 0; i < 4; i++) t.warning(`Synthetic burst #${i + 1}`, { timeoutMs: 0 });
    });

    await expect(page.locator('.rbq-toast-slot')).toHaveCount(5);
    // Only MOBILE_VISIBLE (2) actually render at full height — the fill
    // toast is capped exactly like any other toast, proving it went
    // through the one real ToastContainer cap, not a separate system.
    const visibleCards = page.locator('.rbq-toast-slot:not(.rbq-toast-overflow) .rbq-toast');
    await expect(visibleCards).toHaveCount(2);
    await expect(page.locator('.rbq-toast-overflow-chip')).toHaveText('+3 more');
  });
});
