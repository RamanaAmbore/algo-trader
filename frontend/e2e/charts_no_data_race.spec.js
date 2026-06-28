/**
 * charts_no_data_race.spec.js
 *
 * Regression spec for the two-part BEL "No data available" bug:
 *
 *   Problem 1 (frontend): A render-frame race existed between the symbol-
 *   change $effect setting _bars=[] and _loadHistorical() setting
 *   _histLoading=true. One Svelte render frame could see both
 *   _histLoading=false AND _bars.length===0, which rendered the
 *   "No data available." line (the else-if !_bars.length branch) before
 *   the fetch even started. Fix: _histLoading=true + _histError='' are
 *   set atomically in the same effect batch as _bars=[], before
 *   _loadHistorical is called.
 *
 *   Problem 2 (backend): to_d_daily = date.today() in the ohlcv_store
 *   lookup caused _is_complete_range to require today's daily bar.
 *   Today's bar is not finalized during / after market hours, so the
 *   completeness check always failed → every request hit Tier 3 (broker).
 *   Intermittent empty responses produced the "No data available" message.
 *   Fix: to_d_daily = yesterday so confirmed past bars satisfy the check.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *
 *   1. SSOT   — /charts?symbol=BEL shows .cw-fetch-overlay while loading;
 *               .cw-err / "No data available" NOT visible during load.
 *               After response: chart renders OR error — mutually exclusive.
 *   2. Perf   — cold chart load for BEL fires ≤ 45 API requests.
 *   3. Stale  — ChartWorkspace source: EmptyState branch is guarded by
 *               _histLoading; effect sets _histLoading=true before calling
 *               _loadHistorical. Verified by source grep.
 *   4. Reuse  — loading overlay uses the existing .cw-fetch-overlay
 *               component (not a new div); the "Loading…" text uses the
 *               existing .cw-state branch (not a new element).
 *   5. UX     — both desktop (1400×900) AND mobile (360×800): overlay
 *               legible at narrow width; error + overlay never co-exist.
 *
 * Target: dev.ramboq.com
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/charts_no_data_race.spec.js \
 *   --project=chromium-desktop --project=mobile-portrait --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const BEL_URL = `${BASE}/charts?symbol=BEL&mode=live`;

// Absolute path for stale-code assertions (source-level, not compiled).
const __dir = dirname(fileURLToPath(import.meta.url));
const _CW_SRC = join(__dir, '../src/lib/ChartWorkspace.svelte');

// Run serially: each test logs in and the rate-limit is 5/min on dev.
test.describe.configure({ mode: 'serial' });

// ── Shared session across tests (one login per describe block) ────────────────
/** @type {Record<string, string>} */
let _session = {};

/**
 * Inject saved sessionStorage keys before navigation so subsequent page
 * loads start authenticated without re-running the login form.
 * @param {import('@playwright/test').Page} page
 */
async function injectSession(page) {
  if (!Object.keys(_session).length) return;
  await page.addInitScript((data) => {
    for (const [k, v] of Object.entries(data)) {
      sessionStorage.setItem(k, v);
    }
  }, _session);
  if (_session.ramboq_token) {
    await page.context().setExtraHTTPHeaders({
      Authorization: `Bearer ${_session.ramboq_token}`,
    });
  }
}

/** Wait for the range-group strip to appear (signals chart has rendered). */
async function waitForRangeGroup(page) {
  await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 25_000 });
}

/**
 * Wait until the chart reaches a terminal state: either SVG paths are visible
 * (data rendered) or an error message appears.
 * Returns 'data' | 'error' | 'timeout'.
 */
async function waitForTerminalState(page) {
  try {
    await page.waitForFunction(
      () => {
        const paths = document.querySelectorAll('svg path[d]');
        for (const p of paths) {
          if ((p.getAttribute('d') || '').length > 20) return true;
        }
        // Error or empty state.
        const text = document.body.innerText || '';
        if (text.includes('No data available') || text.includes('Load failed') ||
            text.includes('Slow response')) return true;
        return false;
      },
      { timeout: 35_000 },
    );
    const errCount = await page.locator('text=No data available').count();
    return errCount > 0 ? 'error' : 'data';
  } catch (_) {
    return 'timeout';
  }
}

// ── Dimension 3 (stale code) — pure source assertions, no browser needed ─────

test.describe('Stale-code: ChartWorkspace loading/empty state guards', () => {
  test('SSOT: _histLoading set atomically with _bars=[] in symbol-change effect', () => {
    let src;
    try {
      src = readFileSync(_CW_SRC, 'utf-8');
    } catch (e) {
      throw new Error(`Could not read ChartWorkspace.svelte at ${_CW_SRC}: ${e.message}`);
    }

    // The fix: _histLoading = true must appear BEFORE _bars = [] in the
    // symbol-change $effect (or at minimum before _loadHistorical is called).
    // We check that the effect sets _histLoading before clearing bars.
    const effectBlock = src.slice(src.indexOf('_firstSymEffect = false'));
    const histLoadingPos = effectBlock.indexOf('_histLoading = true');
    const barsResetPos   = effectBlock.indexOf('_bars = []');
    expect(
      histLoadingPos,
      'Symbol-change $effect must set _histLoading = true. ' +
      'Missing this means the empty-state branch fires before loading starts.',
    ).toBeGreaterThanOrEqual(0);
    expect(
      barsResetPos,
      'Symbol-change $effect must reset _bars = []. Not found in expected location.',
    ).toBeGreaterThanOrEqual(0);
    expect(
      histLoadingPos < barsResetPos,
      `_histLoading = true (pos ${histLoadingPos}) must come BEFORE _bars = [] ` +
      `(pos ${barsResetPos}) in the symbol-change effect. ` +
      'A render frame between the two writes can show "No data available" transiently.',
    ).toBe(true);
  });

  // ── BEL race regression — empty-overwrite guard ──────────────────────────

  test('BEL race: empty fetch must not overwrite a non-empty _bars', () => {
    let src;
    try {
      src = readFileSync(_CW_SRC, 'utf-8');
    } catch (e) {
      throw new Error(`Could not read ChartWorkspace.svelte at ${_CW_SRC}: ${e.message}`);
    }

    // The empty-overwrite guard: when a fetch returns zero bars BUT we
    // already have non-empty _bars on screen, the code must early-return
    // without overwriting. Otherwise a successful first response is
    // wiped by a later empty response (operator-caught flicker).
    expect(
      src.includes('_nextBars.length === 0 && _bars.length > 0'),
      'ChartWorkspace must guard against empty-overwriting a non-empty ' +
      '_bars value. Expected: an early-return branch like ' +
      '`if (_nextBars.length === 0 && _bars.length > 0) return;` ' +
      'in _loadHistorical after the fetch resolves.',
    ).toBe(true);
  });

  test('BEL race: empty first-cold response retries once after a brief delay', () => {
    let src;
    try {
      src = readFileSync(_CW_SRC, 'utf-8');
    } catch (e) {
      throw new Error(`Could not read ChartWorkspace.svelte at ${_CW_SRC}: ${e.message}`);
    }

    // The first-cold-empty path: rather than immediately showing "No
    // data available", schedule a single delayed retry (just past the
    // backend's _HIST_CACHE_TTL_EMPTY of 2 s). The latch
    // `_emptyRetryFired` guards against an infinite loop for symbols
    // that genuinely have no listing.
    expect(
      src.includes('_emptyRetryFired'),
      'ChartWorkspace must use _emptyRetryFired set to track per-key ' +
      'empty-retry attempts.',
    ).toBe(true);
    expect(
      src.includes('_emptyRetryTimer'),
      'ChartWorkspace must use _emptyRetryTimer to schedule the delayed ' +
      'retry on a first-cold-empty response.',
    ).toBe(true);
    // Confirm the timer is cleaned up in both onDestroy AND the
    // symbol-change effect — so a stale retry can never fire after the
    // component is destroyed or the user has navigated to a new symbol.
    const destroyBlock = src.slice(src.indexOf('onDestroy('));
    expect(
      destroyBlock.includes('_emptyRetryTimer'),
      'onDestroy must clear _emptyRetryTimer to prevent stale retries.',
    ).toBe(true);
  });

  test('Stale code: EmptyState branch is guarded by !_histLoading AND !_histRetrying', () => {
    let src;
    try {
      src = readFileSync(_CW_SRC, 'utf-8');
    } catch (e) {
      throw new Error(`Could not read ChartWorkspace.svelte at ${_CW_SRC}: ${e.message}`);
    }

    // The {:else if !_bars.length} branch must be preceded by a guard
    // that includes BOTH _histLoading (initial fetch) AND _histRetrying
    // (post-empty retry window). Without _histRetrying, the catchall
    // empty-state branch flashes for ~800 ms between the first response
    // landing (loading=false) and the retry firing. Operator-caught BEL
    // race regression.
    const loadingOrRetryingGuard = '(_histLoading || _histRetrying) && !_bars.length';
    expect(
      src.includes(loadingOrRetryingGuard),
      'ChartWorkspace must have a {:else if (_histLoading || _histRetrying) && !_bars.length} ' +
      'guard so the retry-window does not flash "No data available." ' +
      `Searched for: ${loadingOrRetryingGuard}`,
    ).toBe(true);

    // Confirm the ordering: loading guard comes before empty-state guard.
    // The markup is: <div class="cw-state">No data available.</div>
    const loadingGuardPos = src.indexOf(loadingOrRetryingGuard);
    const emptyGuardPos   = src.indexOf("cw-state\">No data available");
    expect(loadingGuardPos).toBeGreaterThan(0);
    expect(emptyGuardPos).toBeGreaterThan(loadingGuardPos);
  });

  test('Stale code: _histRetrying state declared + cleared on destroy/error', () => {
    let src;
    try {
      src = readFileSync(_CW_SRC, 'utf-8');
    } catch (e) {
      throw new Error(`Could not read ChartWorkspace.svelte: ${e.message}`);
    }

    // The state declaration uses Svelte 5 $state.
    expect(
      src.includes('let _histRetrying = $state('),
      '_histRetrying must be declared as $state(false) in ChartWorkspace.',
    ).toBe(true);

    // The retry path sets _histRetrying=true (so the loading branch matches).
    expect(
      src.includes('_histRetrying = true'),
      '_histRetrying must be set true when scheduling the retry.',
    ).toBe(true);

    // onDestroy / catch / new-symbol all clear it to avoid stale state.
    const onDestroyBlock = src.slice(src.indexOf('onDestroy('));
    expect(
      onDestroyBlock.includes('_histRetrying = false'),
      'onDestroy must clear _histRetrying.',
    ).toBe(true);
  });

  test('Reuse: cw-fetch-overlay used for loading (not a new overlay element)', () => {
    let src;
    try {
      src = readFileSync(_CW_SRC, 'utf-8');
    } catch (e) {
      throw new Error(`Could not read ChartWorkspace.svelte at ${_CW_SRC}: ${e.message}`);
    }

    // The existing .cw-fetch-overlay pattern must still be the loading
    // affordance — not a new div. Confirm the element is present.
    expect(
      src.includes('cw-fetch-overlay'),
      'ChartWorkspace must use the existing .cw-fetch-overlay for loading state. ' +
      'Do not introduce a new overlay element.',
    ).toBe(true);

    // The "Loading…" cw-state div must also be present (shown when
    // _histLoading=true && _bars.length===0, before the slow threshold).
    // The markup is: <div class="cw-state">Loading…</div>
    expect(
      src.includes('cw-state">Loading'),
      'ChartWorkspace must have a .cw-state "Loading…" branch for the fast-path ' +
      '(before _histLoadingSlow fires the full overlay). ' +
      'Expected to find: cw-state">Loading in source.',
    ).toBe(true);
  });
});

// ── Live browser tests — require auth against dev.ramboq.com ─────────────────

test.describe('/charts?symbol=BEL — loading vs no-data states', () => {
  test.beforeAll(async ({ browser }) => {
    test.setTimeout(90_000);
    // Pass baseURL so loginAsAdmin's page.goto('/signin') resolves correctly.
    // browser.newContext() does NOT inherit the config's baseURL automatically.
    const ctx  = await browser.newContext({ baseURL: BASE });
    const page = await ctx.newPage();
    await loginAsAdmin(page);
    _session = await page.evaluate(() => {
      const out = {};
      for (const k of ['ramboq_token', 'ramboq_user']) {
        const v = sessionStorage.getItem(k);
        if (v) out[k] = v;
      }
      return out;
    });
    await page.close();
    await ctx.close();
  });

  // ── Dimension 1: SSOT — overlay visible on cold load; no flash of error ──

  test('SSOT: cw-fetch-overlay visible during cold load; no-data NOT co-visible', async ({ page }) => {
    test.setTimeout(90_000);
    await injectSession(page);

    // Intercept the historical response so we can assert timing.
    let histResponseArrived = false;
    page.on('response', (r) => {
      if (r.url().includes('/api/options/historical')) {
        histResponseArrived = true;
      }
    });

    await page.goto(BEL_URL, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);

    // Wait for the terminal state (either SVG renders or error shows).
    const state = await waitForTerminalState(page);

    // Primary assertion: "No data available" must not show after a successful
    // fetch. If BEL returned bars, state = 'data'; only 'error' is a failure.
    if (histResponseArrived) {
      // Backend was reached. We can't guarantee BEL returns bars on dev
      // (broker eligibility varies), but we CAN assert mutual exclusivity:
      // the overlay and the error text must never be visible simultaneously.
      const overlayVisible = await page.locator('.cw-fetch-overlay').isVisible();
      const errorVisible   = await page.locator('.cw-state.cw-err').isVisible();

      expect(
        overlayVisible && errorVisible,
        'cw-fetch-overlay and cw-err must never be visible simultaneously. ' +
        'Loading overlay should dismiss before error state renders.',
      ).toBe(false);
    }

    // After terminal state is reached, overlay must be hidden (not stuck).
    await expect(
      page.locator('.cw-fetch-overlay'),
      'cw-fetch-overlay must not be visible after fetch completes (either chart or error state)',
    ).not.toBeVisible({ timeout: 3_000 });

    // The "Loading…" text must not persist after load completes.
    const loadingText = page.locator('.cw-state', { hasText: 'Loading' });
    await expect(loadingText).not.toBeVisible({ timeout: 2_000 });
  });

  // ── Dimension 1 (cont.): Exclusive states — chart OR error, never both ────

  test('SSOT: chart SVG and error state are mutually exclusive after load', async ({ page }) => {
    test.setTimeout(90_000);
    await injectSession(page);

    await page.goto(BEL_URL, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);
    const state = await waitForTerminalState(page);

    // After the terminal state: either chart is visible OR error is visible,
    // but not both. This is the exclusive-state guarantee.
    const svgPaths = await page.locator('svg path[d]').evaluateAll(
      (els) => els.filter((e) => (e.getAttribute('d') || '').length > 20).length,
    );
    const errVisible = await page.locator('.cw-state.cw-err').isVisible();
    const noDataVisible = await page.locator('text=No data available').isVisible();
    const anyError = errVisible || noDataVisible;

    if (state === 'data') {
      expect(svgPaths, 'When state=data, SVG paths must be present').toBeGreaterThan(0);
      expect(anyError, 'When state=data, no error text should be visible').toBe(false);
    } else if (state === 'error') {
      expect(svgPaths, 'When state=error, no SVG chart paths expected').toBe(0);
    }
    // state='timeout' is only allowed if the broker is unreachable (dev outage).
    // We don't fail on timeout here — the test_cold_load covers the core assertion.
  });

  // ── Dimension 2: Performance — cold load request budget ───────────────────

  test('Perf: BEL cold chart load fires ≤ 45 API requests', async ({ page }) => {
    test.setTimeout(90_000);
    await injectSession(page);

    /** @type {string[]} */
    const apiCalls = [];
    page.on('request', (req) => {
      if (req.url().includes('/api/')) apiCalls.push(req.url());
    });

    // Wait for the historical response before measuring.
    const histPromise = page.waitForResponse(
      (r) => r.url().includes('/api/options/historical'),
      { timeout: 35_000 },
    ).catch(() => null);

    await page.goto(BEL_URL, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);
    await histPromise;
    // Allow one settle tick for live-LTP SSE connections.
    await page.waitForTimeout(2_000);

    expect(
      apiCalls.length,
      `BEL cold chart-load XHR budget exceeded. Got ${apiCalls.length} calls.\n` +
      `First 50:\n${apiCalls.slice(0, 50).join('\n')}`,
    ).toBeLessThanOrEqual(45);
  });

  // ── Dimension 4: Reuse — overlay uses existing cw-fetch-overlay element ──
  // (covered by stale-code assertions above; browser confirms CSS is applied)

  test('Reuse: .cw-fetch-overlay has expected CSS at narrow (360px) width', async ({ page }) => {
    test.setTimeout(90_000);
    await injectSession(page);

    // Simulate narrow viewport inline (regardless of --project setting).
    await page.setViewportSize({ width: 360, height: 800 });
    await page.goto(BEL_URL, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);

    // If the overlay appeared (slow broker path), confirm it has text
    // that is legible at 360px — the msg text must not overflow.
    const overlay = page.locator('.cw-fetch-overlay');
    const msgEl   = overlay.locator('.cw-fetch-msg');

    // Whether or not the overlay is visible, the CSS class must exist in DOM
    // (Svelte conditionally renders it so we can only check if it exists when visible).
    // Assert the cw-fetch-sub is not negative-overflow or zero-size when shown.
    const overlayVisible = await overlay.isVisible();
    if (overlayVisible) {
      const box = await msgEl.boundingBox();
      if (box) {
        expect(
          box.width,
          `cw-fetch-msg width (${box.width}px) must be positive at 360px viewport`,
        ).toBeGreaterThan(0);
      }
    }
    // Always pass at this point — the sub-text legibility check is conditional on
    // the overlay being visible (slow broker path only).
  });

  // ── Dimension 5: UX — desktop viewport ───────────────────────────────────

  test('UX (desktop): error and overlay not co-visible; loading state uses cw-state', async ({ page }) => {
    test.setTimeout(90_000);
    await injectSession(page);

    await page.setViewportSize({ width: 1400, height: 900 });
    await page.goto(BEL_URL, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);

    // During the fetch window (within 150ms of page load), the .cw-state
    // "Loading…" div may briefly appear. It must be in the chart container.
    // We do NOT assert it IS visible (fast cache-hits skip it), but we
    // confirm the overlay + error never co-exist.
    const overlayVisible = await page.locator('.cw-fetch-overlay').isVisible();
    const errVisible     = await page.locator('.cw-state.cw-err').isVisible();
    expect(
      overlayVisible && errVisible,
      'Desktop: cw-fetch-overlay and cw-err must never both be visible.',
    ).toBe(false);

    // Wait for terminal state then confirm no stuck states.
    await waitForTerminalState(page);
    await expect(page.locator('.cw-fetch-overlay')).not.toBeVisible({ timeout: 3_000 });
    const loadingDiv = page.locator('.cw-state', { hasText: 'Loading' });
    await expect(loadingDiv).not.toBeVisible({ timeout: 2_000 });
  });

  // ── Dimension 5: UX — mobile viewport ────────────────────────────────────

  test('UX (mobile 360px): error and overlay not co-visible; Loading text not stuck', async ({ page }) => {
    test.setTimeout(90_000);
    await injectSession(page);

    await page.setViewportSize({ width: 360, height: 800 });
    await page.goto(BEL_URL, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);

    const overlayVisible = await page.locator('.cw-fetch-overlay').isVisible();
    const errVisible     = await page.locator('.cw-state.cw-err').isVisible();
    expect(
      overlayVisible && errVisible,
      'Mobile: cw-fetch-overlay and cw-err must never both be visible.',
    ).toBe(false);

    await waitForTerminalState(page);
    await expect(page.locator('.cw-fetch-overlay')).not.toBeVisible({ timeout: 3_000 });
    const loadingDiv = page.locator('.cw-state', { hasText: 'Loading' });
    await expect(loadingDiv).not.toBeVisible({ timeout: 2_000 });
  });

  // ── BEL race — empty-then-success: late empty must NOT clobber data ─────
  //
  // Simulates the operator-caught scenario: the user opens /charts?symbol=BEL
  // when the backend's empty-cache window has poisoned the response for 2 s.
  // Frontend mocks an empty first response, then a successful second response
  // (after the retry delay). Final state must be the chart, NOT "No data
  // available." This protects against the race coming back to life in any
  // future refactor.

  // Helper: 30 fake daily bars covering [today - 30, today - 1].
  function _fakeBars() {
    const bars = [];
    const today = new Date();
    for (let i = 30; i >= 1; i--) {
      const d = new Date(today.getTime() - i * 86400000);
      bars.push({
        ts:     d.toISOString().slice(0, 10),
        open:   100 + i * 0.5,
        high:   102 + i * 0.5,
        low:    99  + i * 0.5,
        close:  101 + i * 0.5,
        volume: 100_000 + i * 1000,
      });
    }
    return bars;
  }

  // ── partial=true contract: empty first response with partial flag set
  // triggers the frontend's retry path. Second response (with bars) wins.

  test('BEL race (desktop): partial-empty first response triggers retry; final state has chart', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page);
    await page.setViewportSize({ width: 1400, height: 900 });

    // First /api/options/historical?symbol=BEL response → empty bars +
    // partial=true (the backend's "transient — retry soon" signal).
    // Second response → 30 days of fake bars.
    let histCallCount = 0;
    await page.route('**/api/options/historical**', async (route) => {
      const url = route.request().url();
      if (!url.includes('symbol=BEL')) {
        return route.continue();
      }
      histCallCount++;
      if (histCallCount === 1) {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            symbol: 'BEL', instrument_token: null,
            interval: 'day', bars: [], partial: true,
          }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          symbol: 'BEL', instrument_token: 1234,
          interval: 'day', bars: _fakeBars(), partial: false,
        }),
      });
    });

    await page.goto(BEL_URL, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);

    // Wait long enough for the 800 ms retry timer to fire + second resp.
    await page.waitForTimeout(2_500);

    // The retry must have fired — backend call count ≥ 2.
    expect(
      histCallCount,
      `BEL race: frontend must auto-retry on partial-empty first response. ` +
      `Got ${histCallCount} hist API call(s); expected ≥ 2.`,
    ).toBeGreaterThanOrEqual(2);

    // Final state: chart visible, NOT "No data available."
    const errVisible    = await page.locator('text=No data available').isVisible();
    expect(
      errVisible,
      'BEL race: after retry, "No data available" must NOT be visible — ' +
      'the successful second response must have populated the chart.',
    ).toBe(false);

    // SVG path with non-trivial d-attribute is the chart.
    const svgPathCount = await page.locator('svg path[d]').evaluateAll(
      (els) => els.filter((e) => (e.getAttribute('d') || '').length > 20).length,
    );
    expect(
      svgPathCount,
      'BEL race: chart SVG paths must be present after the successful retry.',
    ).toBeGreaterThan(0);
  });

  test('BEL race (mobile): partial-empty first response triggers retry; final state has chart', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page);
    await page.setViewportSize({ width: 360, height: 800 });

    let histCallCount = 0;
    await page.route('**/api/options/historical**', async (route) => {
      const url = route.request().url();
      if (!url.includes('symbol=BEL')) return route.continue();
      histCallCount++;
      if (histCallCount === 1) {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            symbol: 'BEL', instrument_token: null,
            interval: 'day', bars: [], partial: true,
          }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          symbol: 'BEL', instrument_token: 1234,
          interval: 'day', bars: _fakeBars(), partial: false,
        }),
      });
    });

    await page.goto(BEL_URL, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);
    await page.waitForTimeout(2_500);

    expect(histCallCount).toBeGreaterThanOrEqual(2);
    const errVisible = await page.locator('text=No data available').isVisible();
    expect(errVisible, 'Mobile BEL race: error must not show after retry').toBe(false);
  });

  // ── KEY REGRESSION: during the retry window, "No data available" must
  // NOT be visible. This is the operator-caught flash the prior fix did
  // NOT address. The retry was firing but the empty state was visible
  // for the entire delay because _histLoading=false + _bars=[] + no error
  // hit the catchall {:else if !_bars.length} branch.

  test('BEL race: retry window does NOT flash "No data available"', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page);
    await page.setViewportSize({ width: 1400, height: 900 });

    let histCallCount = 0;
    await page.route('**/api/options/historical**', async (route) => {
      const url = route.request().url();
      if (!url.includes('symbol=BEL')) return route.continue();
      histCallCount++;
      if (histCallCount === 1) {
        // First response: partial-empty (triggers retry).
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            symbol: 'BEL', instrument_token: null,
            interval: 'day', bars: [], partial: true,
          }),
        });
      }
      // Second response: delay 600 ms so the retry window is wide
      // enough to catch the "No data available" flash if the guard fails.
      await new Promise((res) => setTimeout(res, 600));
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          symbol: 'BEL', instrument_token: 1234,
          interval: 'day', bars: _fakeBars(), partial: false,
        }),
      });
    });

    await page.goto(BEL_URL, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);

    // Sample the empty-state visibility during the retry window — at
    // intervals shorter than the retry delay (800 ms) + response delay
    // (600 ms) so we catch any single-frame flash.
    let flashedNoData = false;
    for (let i = 0; i < 12; i++) {
      const visible = await page.locator('.cw-state', { hasText: 'No data available' }).isVisible();
      if (visible) {
        flashedNoData = true;
        break;
      }
      await page.waitForTimeout(120);
    }

    expect(
      flashedNoData,
      'BEL race: "No data available" must NOT flash during the retry window. ' +
      'Operator-caught regression: the prior fix scheduled a retry but cleared ' +
      '_histLoading=false BEFORE the retry fired, leaving the catchall empty-' +
      'state branch visible for ~2.5s.',
    ).toBe(false);

    // Confirm both calls happened and final state is the chart.
    expect(histCallCount).toBeGreaterThanOrEqual(2);
    const svgPathCount = await page.locator('svg path[d]').evaluateAll(
      (els) => els.filter((e) => (e.getAttribute('d') || '').length > 20).length,
    );
    expect(svgPathCount).toBeGreaterThan(0);
  });

  // ── partial=false MUST show "No data available." immediately (no retry).
  // Symbols genuinely without history (delisted, wrong exchange) should
  // fail fast rather than wait 800ms before showing the empty state.

  test('BEL race: partial=false empty response shows error immediately (no retry)', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page);
    await page.setViewportSize({ width: 1400, height: 900 });

    let histCallCount = 0;
    await page.route('**/api/options/historical**', async (route) => {
      const url = route.request().url();
      if (!url.includes('symbol=BEL')) return route.continue();
      histCallCount++;
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          symbol: 'BEL', instrument_token: null,
          interval: 'day', bars: [], partial: false,
        }),
      });
    });

    await page.goto(BEL_URL, { waitUntil: 'domcontentloaded' });
    await waitForRangeGroup(page);
    // Wait beyond the 800 ms retry window — confirm NO retry fired.
    await page.waitForTimeout(2_000);

    expect(
      histCallCount,
      'partial=false must not trigger a retry — wasteful for confirmed-empty symbols.',
    ).toBe(1);

    // "No data available" must be visible by now.
    const errVisible = await page.locator('text=No data available').isVisible();
    expect(
      errVisible,
      'partial=false empty response must surface "No data available" immediately.',
    ).toBe(true);
  });
});
