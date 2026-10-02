/**
 * log_panel_dual_fetch_failure_no_spurious_refresh.spec.js
 *
 * Fix 4 (2026-10) — LogPanel.svelte's `_loadOrders()` merge step fed
 * `noteOrderPollFills()` an unguarded `merged` array even when the
 * BROKER half of the two parallel order fetches failed. Investigation
 * found the literal "both fetches reject" shape is already safe (merged
 * becomes `[]`, which `noteOrderPollFills`'s own `rows.length === 0`
 * early-return already no-ops on without touching its `_lastStatus`
 * map). The REAL spurious-refire path is the asymmetric one: broker
 * fetch rejects while the algo-only fetch still succeeds. Because
 * `brokerIds` is built only from `brokerRows`, a failed broker fetch
 * makes that Set empty, so EVERY algo-only row — including one that
 * mirrors an order_id the detector already recorded as COMPLETE via the
 * broker channel — leaks into `merged` unfiltered. If that algo row's
 * own `.status` field still lags the broker's real-time COMPLETE (a
 * normal transient window before postback/reconcile catches it up),
 * `noteOrderPollFills` downgrades that order_id's `_lastStatus` entry to
 * the algo row's stale non-filled status. The next good poll (broker
 * succeeds again, same order still COMPLETE) then looks exactly like a
 * brand-new OPEN->COMPLETE transition and spuriously bumps `bookChanged`
 * — forcing every subscriber (Legs/Payoff, MarketPulse, PerformancePage,
 * dashboard hero) to force-refresh for no real reason.
 *
 * Fix: skip `noteOrderPollFills` (and the attach-observation toast scan)
 * entirely whenever `brokerResp` is not `fulfilled`, mirroring the exact
 * guard `orderFillPoller.js` already uses for the SAME merge shape.
 *
 * Observability: `bookChanged.js` now mirrors the `bookChanged` counter
 * onto `window.__bookChangedCount` on dev/localhost hosts (added as part
 * of this fix) — lets this spec assert the exact bump count without
 * depending on a page-specific downstream side effect.
 *
 * Five quality dimensions:
 *  1. SSOT    — `noteOrderPollFills`'s shared `_lastStatus` map (one
 *               module-level Map for every caller) is the thing under
 *               test; this guards it from a LogPanel-only corruption path.
 *  2. Perf    — bounded to a handful of 3s poll ticks, no long waits.
 *  3. Stale   — negative control re-proves the detector still fires for
 *               a GENUINE OPEN->COMPLETE transition after the same
 *               sequence, so the fix doesn't just suppress all firing.
 *  4. Reuse   — exercises the exact shared `noteOrderPollFills` /
 *               `bookChanged` modules every other fill-detection surface
 *               (OrderBook.svelte, orderFillPoller.js) also drives.
 *  5. UX      — the end state an operator would otherwise see is a
 *               books/Legs/Payoff grid refreshing itself with no real
 *               new fill — this spec is the regression guard for that.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *   e2e/log_panel_dual_fetch_failure_no_spurious_refresh.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(60_000);

const ORDER_ID = 'LP-9001';
const ACCOUNT  = 'ZG0790';

function _nowIstTimestamp() {
  const shifted = new Date(Date.now() + 5.5 * 60 * 60 * 1000);
  return shifted.toISOString().slice(0, 19).replace('T', ' ');
}

/** @typedef {{ brokerMode: 'ok'|'fail', brokerStatus: string, algoStatus: string }} PollState */

/** @param {import('@playwright/test').Page} page @param {PollState} state */
async function _mockOrderEndpoints(page, state) {
  await page.route('**/api/orders/', (route) => {
    if (state.brokerMode === 'fail') {
      route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'broker timeout' }) });
      return;
    }
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        rows: [{
          order_id: ORDER_ID, tradingsymbol: 'RELIANCE', exchange: 'NSE',
          transaction_type: 'BUY', quantity: 1, price: 1400, average_price: 1400,
          status: state.brokerStatus, account: ACCOUNT, order_timestamp: _nowIstTimestamp(),
        }],
      }),
    });
  });
  // Algo-only row sharing the SAME order_id — simulates the AlgoOrder
  // mirror of the same broker order, whose own .status can transiently
  // lag the broker's real-time value.
  await page.route('**/api/orders/algo/recent**', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify([{
        id: ORDER_ID, order_id: ORDER_ID, tradingsymbol: 'RELIANCE',
        status: state.algoStatus, account: ACCOUNT, created_at: _nowIstTimestamp(),
      }]),
    });
  });
  await page.route('**/api/orders/gtts/**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ gtts: [] }) });
  });
  await page.route('**/api/orders/events**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) });
  });
}

async function _bookChangedCount(page) {
  return page.evaluate(() => /** @type {any} */ (window).__bookChangedCount ?? 0);
}

test.describe('LogPanel _loadOrders — broker-fetch-failure guard on the fill detector', () => {
  test('a broker-fetch-failure tick (algo-only stale status leaking in) does not spuriously bump bookChanged on the next good poll', async ({ page }) => {
    await loginAsAdmin(page);

    const state = { brokerMode: 'ok', brokerStatus: 'COMPLETE', algoStatus: 'COMPLETE' };
    await _mockOrderEndpoints(page, state);

    await page.goto('/activity', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('text=/Order/i').first()).toBeVisible({ timeout: 15_000 });

    // Poll #1 (immediate, on mount) — seeds the shared detector with
    // order_id=LP-9001 already COMPLETE. Seeding never bumps the bus.
    await page.waitForTimeout(500);
    const afterSeed = await _bookChangedCount(page);

    // Poll #2 — broker fetch fails; algo-only row still reports a STALE
    // OPEN status for the same order_id. Without the fix, this would
    // leak into `merged` unfiltered and downgrade _lastStatus to OPEN.
    state.brokerMode = 'fail';
    state.algoStatus = 'OPEN';
    await page.waitForTimeout(3_500); // one poll tick (pollMs=3000) + margin

    // Poll #3 — broker recovers, same order still COMPLETE (no genuine
    // new fill happened). Without the fix this looks like a brand-new
    // OPEN->COMPLETE transition and bumps bookChanged spuriously.
    state.brokerMode = 'ok';
    state.brokerStatus = 'COMPLETE';
    await page.waitForTimeout(3_500);

    const afterRecovery = await _bookChangedCount(page);
    expect(
      afterRecovery, 'bookChanged bumped after a broker-fetch-failure tick with no genuine new fill — spurious refire regression'
    ).toBe(afterSeed);
  });

  test('negative control: a genuine OPEN->COMPLETE transition after the same failure sequence still fires', async ({ page }) => {
    await loginAsAdmin(page);

    const state = { brokerMode: 'ok', brokerStatus: 'OPEN', algoStatus: 'OPEN' };
    await _mockOrderEndpoints(page, state);

    await page.goto('/activity', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('text=/Order/i').first()).toBeVisible({ timeout: 15_000 });

    await page.waitForTimeout(500); // seed — OPEN, no fire
    const beforeFill = await _bookChangedCount(page);

    // A broker-fetch-failure tick happens in between (same exposure as
    // the primary test) but carries no bearing on the order's real state.
    state.brokerMode = 'fail';
    await page.waitForTimeout(3_500);
    state.brokerMode = 'ok';

    // Genuine fill — broker now reports COMPLETE.
    state.brokerStatus = 'COMPLETE';
    state.algoStatus = 'COMPLETE';
    await page.waitForTimeout(3_500);

    const afterFill = await _bookChangedCount(page);
    expect(
      afterFill, 'a genuine OPEN->COMPLETE transition must still bump bookChanged even after an intervening broker-fetch-failure tick'
    ).toBeGreaterThan(beforeFill);
  });
});
