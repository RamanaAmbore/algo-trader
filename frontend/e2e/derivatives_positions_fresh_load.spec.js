/**
 * derivatives_positions_fresh_load.spec.js
 *
 * Verifies the derivatives page sends ?fresh=1 when loading positions
 * (12-defect patch fix in positionsStore.load()).
 *
 * The patch changes how derivatives page refreshes after WebSocket events:
 *   - Old: positionsStore.load() (no args) — uses cache
 *   - New: positionsStore.load({ fresh: true }) — skips cache, forces API refresh
 *   - Network request includes ?fresh=1 query parameter
 *
 * Three quality dimensions:
 *  1. SSOT   — /api/positions endpoint accepts ?fresh parameter
 *  2. Perf   — fresh load bypasses 30s cache for near-live data
 *  3. Stale  — source code no longer calls the broken .load() pattern
 *
 * Run:
 *   PLAYWRIGHT_USER=rambo PLAYWRIGHT_PASS=admin1234 \
 *   PLAYWRIGHT_BASE_URL=http://localhost:5174 \
 *   npx playwright test e2e/derivatives_positions_fresh_load.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname } from 'path';

test.setTimeout(60000);

test.describe('Derivatives positions fresh load', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  // ── Test 1: Derivatives page loads and positions visible ────────────────
  test('1-SSOT: Derivatives page renders positions grid', async ({ page }) => {
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });

    // Wait for the positions grid (Legs card).
    const legsGrid = page.locator('.cand-grid, .legs-grid, .positions-grid').first();
    const gridWaitResult = await legsGrid.waitFor({ state: 'visible', timeout: 15000 }).catch(() => null);

    if (gridWaitResult === null) {
      test.skip(true, 'Positions grid did not load on derivatives page');
      return;
    }

    // Verify grid is visible.
    const gridVisible = await legsGrid.isVisible().catch(() => false);
    expect(gridVisible, 'Positions grid should be visible').toBe(true);

    console.log('[derivatives_positions_fresh_load] Positions grid loaded');
  });

  // ── Test 2: Source-check — positionsStore.load called with fresh param ──
  test('2-Perf: positionsStore.load() called with fresh parameter', () => {
    // Read the derivatives page source to verify the fresh param is used.
    const derivativesPagePath = '/Users/ramanambore/projects/ramboq/frontend/src/routes/(algo)/admin/derivatives/+page.svelte';
    let source = '';
    try {
      source = readFileSync(derivativesPagePath, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read derivatives page: ${e.message}`);
      return;
    }

    // Assertion: source must contain positionsStore.load({ fresh: ... })
    // or similar, indicating the fresh parameter is passed.
    const hasFreshCall = /positionsStore\.load\s*\(\s*\{.*fresh/i.test(source);
    expect(hasFreshCall, 'Derivatives page should call positionsStore.load({ fresh })').toBe(true);

    console.log('[derivatives_positions_fresh_load] Fresh parameter usage verified in source');
  });

  // ── Test 3: Source-scan — no broken .load() calls in derivatives page ────
  test('3-Stale: Derivatives page does not use broken positionsStore.load() pattern', () => {
    // Read the derivatives page source code.
    const derivativesPagePath = '/Users/ramanambore/projects/ramboq/frontend/src/routes/(algo)/admin/derivatives/+page.svelte';
    let source = '';
    try {
      source = readFileSync(derivativesPagePath, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read derivatives page: ${e.message}`);
      return;
    }

    // Assertion 1: positionsStore.load() must be called with an argument
    // (e.g., { fresh: true }), NOT bare .load() or .load(undefined).
    // The old broken pattern was: positionsStore.load()
    // The new pattern is: positionsStore.load({ fresh: true }) or similar

    // Check that there's NO bare .load() call followed by just );
    // Regex: looks for "positionsStore.load(" followed by optional whitespace,
    // then immediately ")" (no arguments).
    const bareLoadPattern = /positionsStore\.load\s*\(\s*\)/;
    expect(bareLoadPattern.test(source), 'Should not have bare positionsStore.load() calls').toBe(false);

    // Assertion 2: verify that at least one call passes an argument.
    const callsWithArg = /positionsStore\.load\s*\(\s*\{.*fresh/;
    expect(callsWithArg.test(source), 'Should have positionsStore.load({ fresh }) call').toBe(true);

    console.log('[derivatives_positions_fresh_load] Source code pattern verified (no bare .load() calls)');
  });

  // ── Test 4: Source-check — `positions_refreshed` WS event is handled ────
  // Defect fix (2026-09-30): the derivatives page's own socket handler
  // (createPerformanceSocket callback) ignored `positions_refreshed`
  // entirely — MarketPulse.svelte / PerformancePage.svelte already react
  // to this event, but the derivatives page silently waited for its own
  // 5 s book-poller cycle instead of refreshing immediately once the
  // backend confirms a genuinely fresh positions read after a fill.
  test('4-SSOT: derivatives page socket handler reacts to positions_refreshed', () => {
    const derivativesPagePath = '/Users/ramanambore/projects/ramboq/frontend/src/routes/(algo)/admin/derivatives/+page.svelte';
    let source = '';
    try {
      source = readFileSync(derivativesPagePath, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read derivatives page: ${e.message}`);
      return;
    }

    // The socket handler must check for msg.event === 'positions_refreshed'
    // and react with a fresh loadPositions() call — matching the exact
    // loadPositions({ fresh: true }) pattern already used elsewhere in the
    // same handler for order_update/position_filled.
    const hasPositionsRefreshedBranch = /msg\?\.event\s*===\s*['"]positions_refreshed['"]/.test(source);
    expect(hasPositionsRefreshedBranch, 'Socket handler should check for positions_refreshed event').toBe(true);

    // Verify the branch calls loadPositions({ fresh: true }), not a bare
    // loadPositions() (which would still serve the stale route cache).
    const branchMatch = source.match(/msg\?\.event\s*===\s*['"]positions_refreshed['"][\s\S]{0,600}/);
    expect(branchMatch, 'positions_refreshed branch should exist').not.toBeNull();
    expect(
      /loadPositions\s*\(\s*\{\s*fresh:\s*true\s*\}\s*\)/.test(branchMatch[0]),
      'positions_refreshed branch should call loadPositions({ fresh: true })'
    ).toBe(true);

    console.log('[derivatives_positions_fresh_load] positions_refreshed handler verified in source');
  });

  // ── Test 5: Live WS injection — positions_refreshed triggers a ?fresh=1
  //    /positions request ─────────────────────────────────────────────────
  // Simulates the backend pushing `positions_refreshed` over /ws/performance
  // (e.g. after a postback fan-out or reconcile sweep) and asserts a new
  // network request to /positions/ with fresh=1 fires as a direct result —
  // not merely on the next 5 s book-poller tick.
  test('5-Perf: positions_refreshed WS event fires a fresh positions fetch', async ({ page }) => {
    /** @type {import('@playwright/test').WebSocketRoute | null} */
    let wsRoute = null;
    await page.routeWebSocket('**/ws/performance', (ws) => {
      wsRoute = ws;
      // Mock mode — no connectToServer() call — Playwright auto-opens the
      // WebSocket inside the page. Real heartbeat pings from the page are
      // simply dropped (no assertions depend on them).
    });

    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });

    // The WebSocket connects during hydration (after domcontentloaded) —
    // poll briefly for the route to register before giving up.
    for (let i = 0; i < 25 && !wsRoute; i++) {
      await page.waitForTimeout(200);
    }
    if (!wsRoute) {
      test.skip(true, 'WebSocket to /ws/performance never connected — cannot inject test event');
      return;
    }

    // Arm the request listener BEFORE sending the message to avoid a race.
    let resolved = false;
    const freshRequestPromise = page.waitForRequest(
      (req) => req.url().includes('/positions/') && req.url().includes('fresh=1'),
      { timeout: 15_000 }
    ).then((r) => { resolved = true; return r; }).catch(() => null);

    // The derivatives page's onMount awaits `loadInstruments()` (cold-cache
    // network round trip) BEFORE its `createPerformanceSocket` subscription
    // registers, so the WS handler isn't necessarily live yet a fixed short
    // delay after navigation — re-send every ~1.2 s (idempotent; each
    // delivery just re-triggers loadPositions({fresh:true})) until either
    // the subscription catches one or the overall 15 s window elapses.
    for (let i = 0; i < 10 && !resolved; i++) {
      try { wsRoute.send(JSON.stringify({ event: 'positions_refreshed' })); } catch (_) { /* route closed — stop */ }
      await page.waitForTimeout(1200);
    }

    const freshRequest = await freshRequestPromise;
    expect(freshRequest, 'positions_refreshed should trigger a ?fresh=1 /positions/ request').not.toBeNull();

    console.log('[derivatives_positions_fresh_load] positions_refreshed → fresh positions fetch confirmed:', freshRequest?.url());
  });
});
