/**
 * quote_stream_survives_pulse_navigation.spec.js
 *
 * Regression spec for the 2026-10-19 incident: MarketPulse.svelte's
 * onDestroy called stopQuoteStream() (and onMount called
 * startQuoteStream()) on the shared SSE singleton in quoteStream.js.
 * Since <MarketPulse> only mounts on /pulse, navigating away from /pulse
 * to ANY other algo page tore down the live-tick stream for the WHOLE
 * app — nothing else ever restarted it while the market stayed open
 * (the 30s market-gate watcher only reopened on a closed→open edge, which
 * never fired since the market never actually closed; the 45s silence
 * watchdog explicitly skips while `_stopped` is true). Every other
 * page's live-tick-dependent surfaces (Legs, Snapshot, PerformancePage,
 * etc.) went permanently frozen until the operator revisited /pulse.
 *
 * Fix (frontend/src/lib/data/quoteStream.js, frontend/src/lib/MarketPulse.svelte):
 *   1. MarketPulse no longer calls startQuoteStream()/stopQuoteStream()
 *      itself — the shared stream's lifecycle belongs solely to the algo
 *      layout's startMarketGatedQuoteStream()/stopMarketGatedQuoteStream().
 *   2. The layout's 30s market-gate watcher was hardened from pure
 *      edge-detection (`anyOpen && !_prevAny`) to state-reconciliation
 *      (`startQuoteStream()` — itself idempotent — called on every tick
 *      the market is open) so this entire bug CLASS self-heals even if a
 *      future page makes the same mistake.
 *
 * Two tests:
 *   1. Client-side navigation away from /pulse (NOT page.goto — a full
 *      reload would remount the layout and restart the stream regardless
 *      of the bug) must not freeze the stream: it keeps reconnecting /
 *      ticking on the destination page.
 *   2. The 30s gate backstop itself: if something external stops the
 *      shared singleton while the market stays open, the gate reopens it
 *      within one 30s tick — exercised directly via a dev-gated
 *      `window.__stores.stopQuoteStream()` hook, since fix #1 means no
 *      production page calls stopQuoteStream() anymore to exercise this
 *      any other way.
 *
 * Run against local dev (default baseURL, picks up uncommitted edits via
 * the vite dev server + /api proxy to dev.ramboq.com):
 *   npx playwright test e2e/quote_stream_survives_pulse_navigation.spec.js --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

const MARKET_OPEN_STATUS = {
  nse_open: true,
  mcx_open: true,
  any_open: true,
  is_holiday: false,
};

// A single real SSE 'tick' event. route.fulfill() delivers this body once
// then ends the response — from the EventSource's perspective this looks
// like the server closed the connection, firing onerror → our manual
// reconnect-with-backoff path (quoteStream.js:_onStreamError). Because a
// tick landed, `_backoffMs` resets to `_BACKOFF_MIN` (2s) before the next
// reconnect, so every ~2s+ a fresh connection (and fresh hit on this
// route) lands for the lifetime of the test.
const SSE_TICK_BODY = 'event: tick\ndata: {"sym":"NIFTY 50","ltp":100.25}\n\n';

/**
 * Click the "Dashboard" nav link via whichever nav surface is visible
 * (desktop inline bar vs. mobile hamburger drawer) — a real client-side
 * `goto()` call inside the Svelte app, never a `page.goto()`/full reload.
 * ONE click + a generous wait (not a click-retry loop): SvelteKit's
 * `goto()` waits for the destination's own `load()` before updating the
 * URL, which can be slow under local multi-worker contention against a
 * shared dev server proxying to a real backend; a second click while
 * that's still in flight risks aborting/restarting the pending
 * navigation and making the delay worse, not better.
 * @param {import('@playwright/test').Page} page
 */
async function navigateClientSideToDashboard(page) {
  const hamburger = page.getByRole('button', { name: 'Toggle menu' });
  if (await hamburger.isVisible().catch(() => false)) {
    await hamburger.click();
    await page.locator('.algo-mobile-item', { hasText: 'Dashboard' }).first().click();
  } else {
    await page.locator('.algo-nav-btn', { hasText: 'Dashboard' }).first().click();
  }
  await page.waitForURL('**/dashboard', { timeout: 30_000 });
}

/** Read the current value of the `lastLiveTickAt` writable store via a
 *  one-shot subscribe/unsubscribe (svelte/store fires the subscriber
 *  synchronously with the current value on subscription). */
function readLastLiveTickAt(/** @type {import('@playwright/test').Page} */ page) {
  return page.evaluate(() => {
    const s = /** @type {any} */ (window).__stores.lastLiveTickAt;
    /** @type {number} */
    let value = 0;
    const unsub = s.subscribe((/** @type {number} */ v) => { value = v; });
    unsub();
    return value;
  });
}

test.describe('SSE quote stream survives leaving /pulse', () => {
  test('stream keeps reconnecting and ticking after a client-side nav away from /pulse', async ({ page }) => {
    await loginAsAdmin(page);

    await page.route('**/api/market/status', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MARKET_OPEN_STATUS),
      });
    });

    let streamHits = 0;
    await page.route('**/api/quotes/stream', async (route) => {
      streamHits++;
      await route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
        body: SSE_TICK_BODY,
      });
    });

    // waitUntil 'domcontentloaded' — SSE keeps 'networkidle' from ever
    // resolving (same precedent as hibernation-refire.spec.js).
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    await page.waitForFunction(() => {
      const s = /** @type {any} */ (window).__stores;
      return !!(s && s.lastLiveTickAt);
    }, { timeout: 15_000 });

    // Let at least one connection + tick land while still on /pulse.
    await page.waitForTimeout(1500);
    expect(streamHits).toBeGreaterThan(0);
    expect(await readLastLiveTickAt(page)).toBeGreaterThan(0);

    await navigateClientSideToDashboard(page);

    // Take the BASELINE snapshot only after the navigation has fully
    // settled (destination page mounted, old /pulse page's MarketPulse
    // instance unmounted) — a URL change can land before Svelte actually
    // tears down the old component tree (view-transition orchestration +
    // the destination page's own `load` can take a couple of seconds),
    // so comparing against a snapshot taken too early would count
    // reconnects that happened while STILL effectively on /pulse as if
    // they'd happened "after" leaving it, masking the exact bug this
    // spec exists to catch. 3s is comfortably past that settle window.
    await page.waitForTimeout(3000);
    const hitsBaseline = streamHits;
    const tickBaseline = await readLastLiveTickAt(page);

    // Give the ~2s backoff reconnect cycle several more rounds to land
    // on the NEW (now fully settled) page.
    await page.waitForTimeout(6000);

    const hitsAfterNav = streamHits;
    const tickAfterNav = await readLastLiveTickAt(page);

    // Pre-fix: MarketPulse's onDestroy called stopQuoteStream() the
    // instant /pulse unmounted — streamHits and lastLiveTickAt would
    // both freeze at their post-settle baseline here, on /dashboard and
    // every other page, until the operator went back to /pulse.
    expect(hitsAfterNav).toBeGreaterThan(hitsBaseline);
    expect(tickAfterNav).toBeGreaterThan(tickBaseline);
  });

  test('30s market-gate backstop reopens the stream if something else stops it while the market stays open', async ({ page }) => {
    await loginAsAdmin(page);

    await page.route('**/api/market/status', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MARKET_OPEN_STATUS),
      });
    });

    let streamHits = 0;
    await page.route('**/api/quotes/stream', async (route) => {
      streamHits++;
      await route.abort();
    });

    // Install + pause the fake clock BEFORE navigation so the 30s gate's
    // own visibleInterval is under our control (same technique as
    // tick_staleness_indicator.spec.js).
    await page.clock.pauseAt(new Date('2026-06-30T06:00:00Z')); // IST 11:30, weekday

    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    await page.waitForFunction(() => {
      const s = /** @type {any} */ (window).__stores;
      return !!(s && typeof s.stopQuoteStream === 'function');
    }, { timeout: 15_000 });

    await page.clock.runFor(500);
    const hitsBefore = streamHits;
    expect(hitsBefore).toBeGreaterThan(0); // boot connect, since market forced open

    // Simulate "something external stopped the shared stream while the
    // market stayed open" — the exact failure class the 30s gate's
    // state-reconciliation backstop now guards against, independent of
    // which page (if any future one) makes the MarketPulse mistake again.
    await page.evaluate(() => /** @type {any} */ (window).__stores.stopQuoteStream());

    // Advance past the 30s market-gate cadence.
    await page.clock.runFor(31_000);
    await page.waitForTimeout(50);

    const hitsAfter = streamHits;
    expect(hitsAfter).toBeGreaterThan(hitsBefore);
  });
});
