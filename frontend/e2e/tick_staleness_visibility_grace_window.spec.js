/**
 * tick_staleness_visibility_grace_window.spec.js
 *
 * Regression spec for a Safari-relevant false-positive in the live-tick
 * staleness watchdog (tickStaleness.svelte.js), found alongside the
 * 2026-10-19 SSE-survives-navigation fix.
 *
 * Bug: stores.js's hibernation `exitHibernation()` runs every registered
 * `visibleInterval` callback SYNCHRONOUSLY and IMMEDIATELY on tab-return
 * — including tickStaleness's own `_check()` — before quoteStream.js's
 * own visibilitychange handler has had a chance to reconnect and deliver
 * a fresh tick. Safari suspends background tabs more aggressively than
 * Chrome, so `_lastTickAt` can be genuinely stale (by real elapsed time)
 * at that exact instant even though the stream recovers correctly a
 * moment later — producing a false "TICKS STALE" badge + toast right on
 * tab return.
 *
 * Fix (frontend/src/lib/data/tickStaleness.svelte.js):
 *   - A capture-phase `visibilitychange` listener on `window` (fires
 *     before any `document`-level target-phase listener, regardless of
 *     registration order) records `_lastVisibleAt` the instant the tab
 *     becomes visible again.
 *   - `_check()` gains an early return while `document.hidden` (a check
 *     landing during the pre-hibernation idle window must never flip the
 *     stale flag for a tab nobody is looking at).
 *   - The staleness gap is now measured from
 *     `Math.max(_lastTickAt, _lastVisibleAt)`, not `_lastTickAt` alone —
 *     a grace window that suppresses a stale verdict until TICK_STALE_MS
 *     has elapsed since EITHER signal, whichever is more recent.
 *
 * Strategy: reproduce the REAL `exitHibernation()` path (not a synthetic
 * shortcut) — same technique as hibernation-refire.spec.js (0ms
 * hibernation threshold via `window.__rbq_hibMs`, `Object.defineProperty`
 * on `document.visibilityState`, a dispatched `visibilitychange` event)
 * combined with tick_staleness_indicator.spec.js's conventions (shrunk
 * `window.__rbq_tickStaleMs`, forced market-open status, aborted SSE
 * route so no real tick can ever land, and a monkey-patched
 * `toast.warning` counter). Real wall-clock waits throughout (not a fake
 * clock) — matching hibernation-refire.spec.js's own precedent for this
 * exact hibernation-timer interaction, which is finicky to drive under a
 * paused clock.
 *
 * Run against local dev:
 *   npx playwright test e2e/tick_staleness_visibility_grace_window.spec.js --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

// Small thresholds so the spec doesn't need to wait a full 75s/5min.
const TEST_STALE_MS = 3_000; // window.__rbq_tickStaleMs
const HIB_MS = 0;            // window.__rbq_hibMs — any hidden tab hibernates "immediately"

const MARKET_OPEN_STATUS = {
  nse_open: true,
  mcx_open: true,
  any_open: true,
  is_holiday: false,
};

test.describe('tick staleness — tab-return grace window (Safari background-tab fix)', () => {
  test.describe.configure({ mode: 'serial' });

  test('no false-positive stale toast the instant the tab returns; genuine staleness still fires once after the grace window elapses', async ({ page }) => {
    await page.addInitScript(({ staleMs, hibMs }) => {
      /** @type {any} */ (window).__rbq_tickStaleMs = staleMs;
      /** @type {any} */ (window).__rbq_hibMs = hibMs;
    }, { staleMs: TEST_STALE_MS, hibMs: HIB_MS });

    await loginAsAdmin(page);

    await page.route('**/api/market/status', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MARKET_OPEN_STATUS),
      });
    });

    // No real tick can ever land — isolates the test entirely to the
    // hibernation/visibility race, matching tick_staleness_indicator.spec.js.
    await page.route('**/api/quotes/stream', (route) => route.abort());

    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    await page.waitForFunction(() => {
      const s = /** @type {any} */ (window).__stores;
      return !!(s && s.toast && typeof s.toast.warning === 'function' && s.lastLiveTickAt);
    }, { timeout: 15_000 });

    await page.evaluate(() => {
      const s = /** @type {any} */ (window).__stores;
      s.__warnCalls = [];
      const orig = s.toast.warning.bind(s.toast);
      s.toast.warning = (/** @type {any} */ msg, /** @type {any} */ opts) => {
        s.__warnCalls.push(msg);
        return orig(msg, opts);
      };
    });
    const warnCount = () => page.evaluate(() => (/** @type {any} */ (window).__stores.__warnCalls || []).length);
    const badge = page.locator('[data-testid="tick-stale-indicator"]').first();

    // Warmup — mount settles, first checker tick scheduled. Well under
    // TEST_STALE_MS so no staleness has genuinely elapsed yet.
    await page.waitForTimeout(400);
    await expect(badge).toHaveCount(0);
    expect(await warnCount()).toBe(0);

    // 1. Hide the tab — with __rbq_hibMs=0 this engages hibernation
    //    (pauses the 5s checker interval) almost immediately.
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
      Object.defineProperty(document, 'hidden', { value: true, configurable: true });
      document.dispatchEvent(new Event('visibilitychange', { bubbles: true }));
    });
    await page.waitForTimeout(150); // let the 0ms hibernation timer fire

    // 2. Stay hidden long enough for real elapsed time to exceed
    //    TEST_STALE_MS — reproducing the exact precondition of the bug:
    //    `_lastTickAt` (fixed at mount, never updated — the SSE route is
    //    aborted) is now genuinely "stale" by wall-clock time, even
    //    though the checker itself is paused (hibernating) throughout
    //    this window and never actually evaluates it.
    await page.waitForTimeout(TEST_STALE_MS + 600);

    // 3. Tab returns. This is the race: stores.js's exitHibernation()
    //    calls every hibernating visibleInterval's callback — including
    //    tickStaleness's _check() — SYNCHRONOUSLY in this same dispatch.
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
      Object.defineProperty(document, 'hidden', { value: false, configurable: true });
      document.dispatchEvent(new Event('visibilitychange', { bubbles: true }));
    });
    await page.waitForTimeout(50); // let Svelte flush any DOM update

    // Pre-fix, this would already be stale=true/warnCount=1 here, because
    // the immediate `_check()` call measured against the long-stale
    // `_lastTickAt` with no grace window. Post-fix, `_lastVisibleAt` was
    // just stamped "now" by the capture-phase listener before
    // exitHibernation's target-phase listener ran `_check()`.
    await expect(badge).toHaveCount(0);
    expect(await warnCount()).toBe(0);

    // 4. Tab stays visible from here on. The checker resumed its normal
    //    5s cadence (exitHibernation's own immediate-refire-then-resume
    //    behavior). Once TEST_STALE_MS genuinely elapses since the
    //    tab-return (still no real tick — stream stays aborted), the
    //    watchdog must still catch the real staleness, exactly once.
    await page.waitForTimeout(TEST_STALE_MS + 1_500);

    await expect(badge.first()).toBeVisible();
    expect(await warnCount()).toBe(1);
  });
});
