/**
 * tick_staleness_indicator.spec.js
 *
 * Verifies the live-tick staleness indicator (RefreshButton.svelte +
 * tickStaleness.svelte.js), added 2026-10 after a backend bug let the
 * KiteTicker→SSE tick bridge die silently for hours while the SSE
 * connection itself (heartbeats) stayed healthy — "LTP looks frozen"
 * with zero on-screen cue.
 *
 * What this tests:
 *   1. The amber "TICKS STALE" badge appears once the market is open AND
 *      no real SSE tick/snapshot ('lastLiveTickAt') has landed for the
 *      configured threshold.
 *   2. Exactly ONE toast fires for the episode — not a flood of toasts
 *      across repeated 5s checker ticks while the stream stays dead.
 *   3. The indicator clears the instant a new tick lands, and the
 *      "toast already fired" flag resets so a future recurrence can
 *      fire its own toast (verified by crossing the threshold a second
 *      time after a tick, confirming the toast counter advances to 2).
 *
 * Strategy (mirrors sparkline_closed_hours.spec.js's conventions):
 *   - page.clock.pauseAt(...) + page.clock.runFor(...) to freeze and then
 *     fast-forward the 5s staleness-checker interval deterministically,
 *     without real-time waiting. NOTE: page.clock.install() alone does
 *     NOT pause time (it only sets the starting instant — time then keeps
 *     flowing at real wall-clock speed); pauseAt() is what actually
 *     freezes it so runFor() is the only thing that advances it.
 *   - window.__rbq_tickStaleMs (addInitScript, same precedent as
 *     stores.js's window.__rbq_hibMs) to shrink the 75s production
 *     threshold to a few seconds for the test.
 *   - page.route('**\/api/market/status') to force the market-open state
 *     deterministically (isNseOpen/isMcxOpen prefer server status over
 *     the time-of-day fallback the moment it lands).
 *   - page.route('**\/api/quotes/stream') aborted so no real SSE tick can
 *     ever land from a live backend, isolating the test to the
 *     `window.__stores.lastLiveTickAt` injection path.
 *   - window.__stores.lastLiveTickAt.set(...) (layout dev/localhost-gated
 *     exposure, same convention as window.__stores.tickBus /
 *     window.__stores.toast) to simulate a real tick arriving.
 *   - window.__stores.toast.warning monkey-patched with a counter so the
 *     assertion doesn't depend on the toast's own 8s auto-dismiss timer
 *     or the mobile toast-burst-cap's display:none rendering.
 *
 * Run against dev:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com npx playwright test \
 *     e2e/tick_staleness_indicator.spec.js --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

// Test-only threshold — well under the production 75s default so the
// spec doesn't need to wait (or fake-advance) a full minute+.
const TEST_STALE_MS = 10_000; // 10s
// Checker cadence (tickStaleness.svelte.js's visibleInterval) is 5s —
// advancing by a bit more than 2x that cadence guarantees at least one
// checker tick lands at/after TEST_STALE_MS.
const ADVANCE_PAST_THRESHOLD_MS = TEST_STALE_MS + 5_000;

const MARKET_OPEN_STATUS = {
  nse_open: true,
  mcx_open: true,
  any_open: true,
  is_holiday: false,
};

test.describe('tick staleness indicator', () => {
  test.describe.configure({ mode: 'serial' });

  let token = '';

  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    const info = await loginAsAdmin(page);
    token = info.token;
    await ctx.close();
  });

  test('badge appears after threshold, fires exactly one toast, clears on tick, re-arms for the next episode', async ({ page }) => {
    // 1. Shrink the staleness threshold for this test (must land before
    //    tickStaleness.svelte.js's module-level TICK_STALE_MS evaluates).
    await page.addInitScript((ms) => {
      /** @type {any} */ (window).__rbq_tickStaleMs = ms;
    }, TEST_STALE_MS);

    // 2. Re-inject auth (loginAsAdmin's addInitScript was bound to the
    //    throwaway beforeAll page/context, not this one).
    await page.addInitScript((tok) => {
      sessionStorage.setItem('ramboq_token', tok);
    }, token);
    await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${token}` });

    // 3. Force the market-open state deterministically — isNseOpen() /
    //    isMcxOpen() prefer this server status over the time-of-day
    //    fallback the instant startMarketStatusPoller's boot fetch lands.
    await page.route('**/api/market/status', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MARKET_OPEN_STATUS),
      });
    });

    // 4. Block the real SSE tick stream entirely — no live backend tick
    //    can ever land, isolating the test to the window.__stores
    //    injection path below.
    await page.route('**/api/quotes/stream', (route) => route.abort());

    // 5. Install AND pause the fake clock BEFORE navigation so the
    //    checker's setInterval (started at mount) is under our control.
    //    `install()` alone only sets the starting instant — time keeps
    //    flowing in real wall-clock speed from there; `pauseAt()` is what
    //    actually freezes it so `runFor()` becomes the only thing that
    //    advances it.
    await page.clock.pauseAt(new Date('2026-06-30T06:00:00Z')); // IST 11:30, weekday

    // 6. Navigate to /pulse (mounts RefreshButton + the layout's
    //    window.__stores exposure).
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle', timeout: 30_000 }).catch(() => {});

    // 7. Wait for the dev/localhost-gated window.__stores exposure
    //    (layout onMount) to land, then monkey-patch toast.warning with
    //    a counter. Patching the SAME object RefreshButton/tickStaleness
    //    import (object identity, not a copy) intercepts the real calls.
    await page.waitForFunction(() => {
      const s = /** @type {any} */ (window).__stores;
      return !!(s && s.toast && typeof s.toast.warning === 'function' && s.lastLiveTickAt);
    }, { timeout: 15_000 });

    await page.evaluate(() => {
      const s = /** @type {any} */ (window).__stores;
      s.__warnCalls = [];
      const orig = s.toast.warning.bind(s.toast);
      s.toast.warning = (msg, opts) => {
        s.__warnCalls.push(msg);
        return orig(msg, opts);
      };
    });

    const warnCount = () => page.evaluate(() => (/** @type {any} */ (window).__stores.__warnCalls || []).length);

    // 8. Let mount settle (initial market-status fetch + first checker
    //    tick scheduling) under the fake clock.
    await page.clock.runFor(500);

    const badge = page.locator('[data-testid="tick-stale-indicator"]').first();

    // Sanity: badge is NOT shown before the threshold elapses.
    await expect(badge).toHaveCount(0);
    expect(await warnCount()).toBe(0);

    // 9. Advance past the threshold — the 5s checker should flip
    //    tickStaleness.stale to true and fire exactly one toast.
    await page.clock.runFor(ADVANCE_PAST_THRESHOLD_MS);
    await page.waitForTimeout(50); // let the Svelte scheduler flush the DOM update

    await expect(badge.first()).toBeVisible();
    expect(await warnCount()).toBe(1);

    // 10. Advance further while the stream STAYS dead — must not flood:
    //     still exactly one toast, badge still visible.
    await page.clock.runFor(ADVANCE_PAST_THRESHOLD_MS);
    await page.waitForTimeout(50);
    expect(await warnCount()).toBe(1);
    await expect(badge.first()).toBeVisible();

    // 11. Simulate a real tick landing via the injected store.
    await page.evaluate(() => {
      /** @type {any} */ (window).__stores.lastLiveTickAt.set(Date.now());
    });
    await page.waitForTimeout(50);

    // Indicator clears immediately on the new tick.
    await expect(page.locator('[data-testid="tick-stale-indicator"]')).toHaveCount(0);

    // 12. A SECOND episode (threshold crossed again after the tick) must
    //     fire its OWN toast — confirms the "toast already fired" flag
    //     was reset, not just a one-time latch.
    await page.clock.runFor(ADVANCE_PAST_THRESHOLD_MS);
    await page.waitForTimeout(50);

    await expect(page.locator('[data-testid="tick-stale-indicator"]').first()).toBeVisible();
    expect(await warnCount()).toBe(2);
  });
});
