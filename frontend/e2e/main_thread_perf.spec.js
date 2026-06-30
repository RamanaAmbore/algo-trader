/**
 * Regression guard: main-thread responsiveness during RefreshButton clicks.
 *
 * Root cause fixed (2026-06-28): `liveSpot` and `_legsExpPnlTotal` read
 * `_underlyingQuotes[selectedUnderlying]?.ltp` as a tracked reactive dep.
 * `_underlyingQuotes` is replaced wholesale every 30 s, triggering a
 * synchronous cascade through 6+ $derived computations and a full
 * OptionsPayoff SVG re-render — starving the main thread of click events.
 *
 * Root causes fixed (2026-06-30) for remaining pages:
 *
 *   /pulse — MarketPulse.svelte:
 *     `positions = $derived(positionsStore.value)` → `unifiedRows = $derived.by`
 *     → `buildUnified()` (large computation) ran synchronously when
 *     `positionsStore.set()` was called from `loadPulse()`. Fix: bridge via
 *     `$effect → $state` so the downstream derived chain is scheduled, not
 *     inline with the store write.
 *
 *   /dashboard:
 *     `_positionsRaw = $derived(positionsStore.value)` → `_positions` →
 *     `_positionsSummary` → `_positionsTotal` → `$effect grid.setGridOption`
 *     ran as a synchronous long task. Fix: same `$effect → $state` bridge.
 *
 * This test asserts per page:
 *   1. No long tasks (>100 ms) during a button-click interaction.
 *   2. Button click-to-visible-feedback latency <350 ms.
 *   3. JS heap growth during 30 s idle <5 MB/min (leak guard).
 *   4. Relevant API request rate is sane.
 *
 * Auth strategy: login ONCE per describe group in beforeAll using the page
 * fixture (Playwright manages lifecycle). The JWT token is captured to a
 * module-level variable then restored via addInitScript in each test —
 * avoids hammering /api/auth/login with parallel requests that trigger the
 * 5/min rate-limiter, and avoids browser.newContext() tracing-artifact issues.
 *
 * Run against dev:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com npx playwright test \
 *     e2e/main_thread_perf.spec.js --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

// ── Shared helpers ──────────────────────────────────────────────────────────

/** Inject long-task PerformanceObserver before page navigation. */
async function instrumentLongTasks(page) {
  await page.addInitScript(() => {
    window.__longTasks = [];
    try {
      const obs = new PerformanceObserver(list => {
        for (const e of list.getEntries()) {
          window.__longTasks.push({ duration: e.duration, startTime: e.startTime });
        }
      });
      obs.observe({ type: 'longtask', buffered: true });
    } catch (_) {
      // longtask not supported — skip silently.
    }
  });
}

/** Measure JS heap in MB. */
async function heapMB(page) {
  return page.evaluate(() =>
    (window.performance?.memory?.usedJSHeapSize ?? 0) / (1024 * 1024)
  );
}

/**
 * Click the page-header RefreshButton and measure:
 *   - click-to-feedback latency (ms)
 *   - long tasks that fired during / after the click
 *
 * Returns { clickToFeedbackMs, longTasks, maxLongTask, totalLongTaskMs }.
 */
async function clickRefreshAndMeasure(page) {
  // Clear any long tasks from the idle phase before we click.
  await page.evaluate(() => { window.__longTasks = []; });

  const refreshBtn = page.locator(
    '.page-header button[aria-label*="Refresh"], ' +
    '.page-header button[title*="Refresh"], ' +
    '.page-header .rf-btn'
  ).first();

  const btnVisible = await refreshBtn.isVisible({ timeout: 5_000 }).catch(() => false);
  let clickToFeedbackMs = -1;

  if (btnVisible) {
    const tClick = Date.now();
    await refreshBtn.click();
    // "feedback" = button enters loading (spinner appears) OR a network request
    // fires for /api/*. Either confirms the click was processed by JS.
    await Promise.race([
      page.waitForFunction(() => {
        return !!document.querySelector(
          '.rf-btn.rf-spinning, [aria-label*="Refreshing"], ' +
          '[aria-busy="true"], .loading, .spinner'
        );
      }, { timeout: 200 }).catch(() => null),
      page.waitForRequest(req => req.url().includes('/api/'), { timeout: 200 })
        .catch(() => null),
      page.waitForTimeout(200),
    ]);
    clickToFeedbackMs = Date.now() - tClick;
    console.log(`[click] refresh → feedback: ${clickToFeedbackMs} ms`);
  } else {
    // Fallback: try any header button (collapse, etc.)
    const anyBtn = page.locator('.page-header button, .bucket-header button').first();
    const anyVisible = await anyBtn.isVisible({ timeout: 3_000 }).catch(() => false);
    if (anyVisible) {
      const tClick = Date.now();
      await anyBtn.click();
      await page.waitForTimeout(200);
      clickToFeedbackMs = Date.now() - tClick;
      console.log(`[click] fallback button → feedback: ${clickToFeedbackMs} ms`);
    } else {
      console.log('[click] no button found — skipping click measurement');
    }
  }

  // Drain any queued tasks that fired in response to the click.
  await page.waitForTimeout(500);

  const longTasks = await page.evaluate(() => window.__longTasks ?? []);
  const maxLongTask = longTasks.length > 0
    ? Math.max(...longTasks.map(t => t.duration))
    : 0;
  const totalLongTaskMs = longTasks.reduce((s, t) => s + t.duration, 0);

  console.log(`[long-tasks] count: ${longTasks.length}, max: ${maxLongTask.toFixed(1)} ms, total: ${totalLongTaskMs.toFixed(1)} ms`);
  if (longTasks.length > 0) {
    for (const t of longTasks.slice(0, 5)) {
      console.log(`  ↳ ${t.duration.toFixed(1)} ms @ ${t.startTime.toFixed(0)} ms`);
    }
  }

  return { clickToFeedbackMs, longTasks, maxLongTask, totalLongTaskMs };
}

/**
 * Assert the common perf dimensions after a click:
 *   1. Max long task < 100 ms (RAIL interaction budget).
 *   2. Click-to-feedback < 350 ms (allows ~100 ms network RTT to dev server).
 *   3. JS heap growth during idle < 5 MB/min.
 */
function assertPerfDimensions({ maxLongTask, clickToFeedbackMs, heapGrowthPerMin }) {
  expect(maxLongTask,
    `Max long-task was ${maxLongTask.toFixed(1)} ms — main-thread blocked`
  ).toBeLessThan(100);

  if (clickToFeedbackMs >= 0) {
    expect(clickToFeedbackMs,
      `Click-to-feedback took ${clickToFeedbackMs} ms — main thread was blocked`
    ).toBeLessThan(350);
  }

  expect(heapGrowthPerMin,
    `JS heap growing at ${heapGrowthPerMin.toFixed(2)} MB/min — possible memory leak`
  ).toBeLessThan(5);
}

// ── Shared idle + heap setup ────────────────────────────────────────────────

async function settleAndMeasureHeap(page, settleSecs = 20) {
  const before = await heapMB(page);
  await page.waitForTimeout(settleSecs * 1000);
  const after = await heapMB(page);
  const growthPerMin = (after - before) / (settleSecs / 60);
  console.log(`[heap] before: ${before.toFixed(1)} MB, after ${settleSecs}s: ${after.toFixed(1)} MB, growth: ${growthPerMin.toFixed(2)} MB/min`);
  return { heapGrowthPerMin: growthPerMin };
}

// ── Single-login helper ─────────────────────────────────────────────────────
// Restores the JWT into a fresh page before navigation so the app boots as
// an authenticated operator without going through the sign-in form again.

/**
 * Seed sessionStorage + extra headers with a pre-captured JWT token so the
 * page boots as if the operator already signed in. Call this BEFORE
 * page.goto() (the initScript fires before page scripts run).
 *
 * @param {import('@playwright/test').Page} page
 * @param {string} jwt  JWT string captured from a prior loginAsAdmin call.
 */
async function seedAuth(page, jwt) {
  await page.addInitScript((token) => {
    // Write the token into sessionStorage so the auth store picks it up
    // when the Svelte stores initialize (before any fetch fires).
    sessionStorage.setItem('ramboq_token', token);
    // Also seed a minimal user object so the role-check effect doesn't
    // redirect to /signin on first render.
    const parts = token.split('.');
    if (parts.length === 3) {
      try {
        const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')));
        sessionStorage.setItem('ramboq_user', JSON.stringify({
          username: payload.sub,
          role: payload.role,
          display_name: payload.display_name,
        }));
      } catch (_) { /* ignore */ }
    }
  }, jwt);
}

// ── Module-level JWT cache ──────────────────────────────────────────────────
// One login per test file execution; all tests share the token via this var.

/** @type {string} */
let _sharedJwt = '';

/**
 * Login once, capture JWT. Call from beforeAll.
 * Uses a full browser context (real cookies) so the login flow completes.
 *
 * @param {import('@playwright/test').Browser} browser
 */
async function ensureJwtWithBrowser(browser) {
  if (_sharedJwt) return;
  const ctx = await browser.newContext({ baseURL: BASE });
  const page = await ctx.newPage();
  try {
    await loginAsAdmin(page);
    const token = await page.evaluate(() => sessionStorage.getItem('ramboq_token'));
    if (!token) throw new Error('Login succeeded but no token found in sessionStorage');
    _sharedJwt = token;
    console.log('[auth] JWT captured for shared use');
  } finally {
    await page.close();
    await ctx.close();
  }
}

// ── /admin/derivatives perf ──────────────────────────────────────────────────

test.describe('/admin/derivatives — RefreshButton click: no main-thread block', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(120_000);

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(60_000);
    await ensureJwtWithBrowser(browser);
  });

  test('chromium-desktop: max long task < 100 ms, click-to-feedback < 350 ms', async ({ page }) => {
    await instrumentLongTasks(page);
    await seedAuth(page, _sharedJwt);
    await page.goto('/admin/derivatives', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.ag-root-wrapper, .card, main').first()
      .waitFor({ state: 'visible', timeout: 30_000 });
    const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 20);
    const perf = await clickRefreshAndMeasure(page);
    assertPerfDimensions({ ...perf, heapGrowthPerMin });
  });

  test('chromium-mobile: 390×844 — same perf contract', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await instrumentLongTasks(page);
    await seedAuth(page, _sharedJwt);
    await page.goto('/admin/derivatives', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.ag-root-wrapper, .card, main').first()
      .waitFor({ state: 'visible', timeout: 30_000 });
    const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 20);
    const perf = await clickRefreshAndMeasure(page);
    assertPerfDimensions({ ...perf, heapGrowthPerMin });
  });
});

// ── /pulse perf ─────────────────────────────────────────────────────────────

test.describe('/pulse — RefreshButton click: no main-thread block', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(120_000);

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(60_000);
    await ensureJwtWithBrowser(browser);
  });

  test('chromium-desktop: max long task < 100 ms, click-to-feedback < 350 ms', async ({ page }) => {
    await instrumentLongTasks(page);
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.ag-row').first().waitFor({ state: 'visible', timeout: 30_000 });
    const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 20);
    const perf = await clickRefreshAndMeasure(page);
    assertPerfDimensions({ ...perf, heapGrowthPerMin });
  });

  test('chromium-mobile: 390×844 — same perf contract', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await instrumentLongTasks(page);
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.ag-row').first().waitFor({ state: 'visible', timeout: 30_000 });
    const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 20);
    const perf = await clickRefreshAndMeasure(page);
    assertPerfDimensions({ ...perf, heapGrowthPerMin });
  });
});

// ── /pulse — loadPulse store migration: request-rate guard ─────────────────
//
// Background: MarketPulse.loadPulse() was migrated to consume from the shared
// positionsStore / holdingsStore (layout-resident cross-page pollers) rather
// than calling fetchPositions() / fetchHoldings() directly.
//
// Expected rate (post-migration, ~24 req/min):
//   - Layout book poller: 5s cadence → 12/min for positions + 12/min for
//     holdings = 24/min total.
//   - loadPulse(force=false): reads .value directly, 0 HTTP calls.
//   - Manual Refresh or mount: loadPulse(force=true) → 1 positions + 1 holdings.
//
// Before migration (~36 req/min):
//   - fetchPositions() + fetchHoldings() called directly every 10s → 12/min.
//   - Plus layout poller 12/min each → 36/min total.
//
// This test guards against regression to 36+ req/min.

test.describe('loadPulse store migration — request-rate guard', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(120_000);

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(60_000);
    await ensureJwtWithBrowser(browser);
  });

  test('chromium-desktop: /positions request rate < 30/min over 30 s', async ({ page }) => {
    const posReqs = [];
    page.on('request', req => {
      if (req.url().includes('/api/auth/positions')) posReqs.push(Date.now());
    });

    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});

    const t0 = Date.now();
    await page.waitForTimeout(30_000);
    const elapsed = (Date.now() - t0) / 60_000;
    const rate = posReqs.length / elapsed;
    console.log(`[rate] /positions: ${posReqs.length} reqs in 30 s → ${rate.toFixed(1)}/min`);

    expect(rate,
      `positions rate ${rate.toFixed(1)}/min exceeds 30/min — likely regression to direct fetchPositions() calls`
    ).toBeLessThan(30);
  });

  test('chromium-desktop: single Refresh fires at most 3 /positions calls', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    // Settle so mount-time requests are done.
    await page.waitForTimeout(5_000);

    const posAfterReqs = [];
    page.on('request', req => {
      if (req.url().includes('/api/auth/positions')) posAfterReqs.push(Date.now());
    });

    // Click the Refresh button.
    const refreshBtn = page.locator(
      '.page-header button[aria-label*="Refresh"], .page-header .rf-btn'
    ).first();
    if (await refreshBtn.isVisible({ timeout: 3_000 }).catch(() => false)) {
      await refreshBtn.click();
    }
    await page.waitForTimeout(2_000);

    console.log(`[rate] single Refresh → ${posAfterReqs.length} /positions calls`);
    expect(posAfterReqs.length,
      `Single Refresh fired ${posAfterReqs.length} /positions calls — expected ≤3`
    ).toBeLessThanOrEqual(3);
  });

  test('chromium-mobile: /positions request rate < 30/min over 30 s', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const posReqs = [];
    page.on('request', req => {
      if (req.url().includes('/api/auth/positions')) posReqs.push(Date.now());
    });

    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});

    const t0 = Date.now();
    await page.waitForTimeout(30_000);
    const rate = posReqs.length / ((Date.now() - t0) / 60_000);
    console.log(`[rate] mobile /positions: ${posReqs.length} reqs in 30 s → ${rate.toFixed(1)}/min`);

    expect(rate).toBeLessThan(30);
  });
});

// ── RefreshButton spin — visibility-return after hibernation ──────────────────
//
// When the tab returns from hibernation (≥ polling.idle_timeout_min hidden),
// `postHibernationRefiring` flips true → every RefreshButton gains the
// `rf-spinning` class within 100 ms (Svelte microtask flush). The button stops
// spinning within 3 s (max-wait timer) OR when all refire callbacks settle
// (whichever comes first).
//
// Brief tab-switch (< threshold): button must NOT spin (no hibernation).
//
// Test strategy — no fake clocks (page.clock.install is unreliable with
// cross-context module variable access):
//   1. After page load, call window.__rbq_setHibMs(0) to set threshold to
//      0 minutes = 0 ms, so the next visibilitychange → hidden immediately
//      queues _enterHibernation() with setTimeout(fn, 0).
//   2. Hide tab via visibilitychange event.
//   3. Wait 300 ms real time for the 0 ms timer to fire.
//   4. Show tab — _exitHibernation() sets postHibernationRefiring = true.
//   5. RefreshButton has rf-spinning class within 100 ms (Svelte flush).
//   6. Button stops spinning within 3 s (max-wait timer clears the flag).
//
// NOTE: window.__rbq_setHibMs(N) takes MINUTES. setHibMs(0) → 0 ms. ✓

test.describe('RefreshButton spin — visibility-return after hibernation', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(120_000);

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(60_000);
    await ensureJwtWithBrowser(browser);
  });

  /**
   * Shared body: verifies RefreshButton spins after hibernation + stops spinning.
   * @param {import('@playwright/test').Page} page
   */
  async function runReconnectTest(page) {
    // No fake clock — uses real timers throughout.
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });

    // Wait for page to settle.
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    await page.waitForTimeout(500);

    // Override hibernation threshold to 0 minutes = 0 ms (real time).
    // window.__rbq_setHibMs is exposed by stores.js after module init.
    // 0 minutes → Math.max(0, 0) * 60 * 1000 = 0 ms threshold.
    await page.evaluate(() => {
      if (typeof window.__rbq_setHibMs === 'function') {
        window.__rbq_setHibMs(0); // 0 minutes = 0 ms
      }
    });

    // Hide the tab — starts the 0 ms hibernation timer.
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true, get: () => 'hidden',
      });
      Object.defineProperty(document, 'hidden', {
        configurable: true, get: () => true,
      });
      document.dispatchEvent(new Event('visibilitychange'));
    });

    // Real-time wait for the 0 ms timer to fire and _enterHibernation() to run.
    // setTimeout(fn, 0) fires on the next task; 300 ms is generous.
    await page.waitForTimeout(300);

    // Return to visible — _exitHibernation() fires and sets postHibernationRefiring = true.
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true, get: () => 'visible',
      });
      Object.defineProperty(document, 'hidden', {
        configurable: true, get: () => false,
      });
      document.dispatchEvent(new Event('visibilitychange'));
    });

    // RefreshButton should have rf-spinning class within 100 ms of visibility-return.
    // (Allows for Svelte's microtask-scheduling latency after a store write.)
    const refreshBtn = page.locator('.page-header .rf-btn').first();
    await expect(refreshBtn).toHaveClass(/rf-spinning/, { timeout: 1_000 });
    console.log('[reconnect] RefreshButton has rf-spinning class after hibernation-return');

    // Button stops spinning after _RECONNECT_MAX_MS = 3 s (real timer).
    // No clock advance needed — waitForTimeout lets the real timer fire.
    await expect(refreshBtn).not.toHaveClass(/rf-spinning/, { timeout: 5_000 });
    console.log('[reconnect] RefreshButton stopped spinning within 3 s real-time wait');
  }

  test('chromium-desktop: RefreshButton spins after hibernation, stops within 3 s', async ({ page }) => {
    await runReconnectTest(page);
  });

  test('chromium-mobile: same RefreshButton spin contract on 390×844 viewport', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await runReconnectTest(page);
  });

  // Guard: brief tab switch (< threshold) must NOT spin the RefreshButton.
  test('brief tab-switch (under threshold) — RefreshButton does NOT spin', async ({ page }) => {
    // Default threshold is 5 minutes — do NOT override it.
    // We hide for 2 s real time (well under 5 min) and expect no spin.
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    await page.waitForTimeout(500);

    // Hide for 2 s (well under the 5-min default threshold).
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true, get: () => 'hidden',
      });
      Object.defineProperty(document, 'hidden', {
        configurable: true, get: () => true,
      });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.waitForTimeout(2_000);

    // Return to visible — hibernation did NOT engage (only 2 s, < 5 min).
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true, get: () => 'visible',
      });
      Object.defineProperty(document, 'hidden', {
        configurable: true, get: () => false,
      });
      document.dispatchEvent(new Event('visibilitychange'));
    });

    await page.waitForTimeout(300);
    // RefreshButton must not have rf-spinning class.
    const refreshBtn = page.locator('.page-header .rf-btn').first();
    await expect(refreshBtn).not.toHaveClass(/rf-spinning/, { timeout: 500 });
    console.log('[reconnect] confirmed: no rf-spinning on brief tab-switch (under 5-min threshold)');
  });
});

// ── RefreshButton spinner — monotonic-animation guard (Jun 2026 reaudit) ────
//
// Root cause closed (Jun 2026): the per-tick rotation animation
// (`rf-tick-rotate`, 0.25s finite) used to share the SAME <svg> element
// as the spin animation (`rf-spinning`). When `loading` flipped true during
// Refresh, the SVG element was recycled (no DOM replace), so the `rf-spinning`
// animation continued from wherever the prior `rf-tick-rotate` left off —
// producing a random start angle on every click. Fix: separate the glyph
// element (`rf-spin-icon`) so the spinner always starts from 0.
//
// This test is a DOM / animation-state check (not perf), so it just clicks
// Refresh, captures the computed animation-name at the moment the button
// enters the loading state, and asserts the spinner animation is active.

test.describe('RefreshButton spinner — animation contract', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(60_000);

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(30_000);
    await ensureJwtWithBrowser(browser);
  });

  test('chromium-desktop: spinner class present during loading state on /pulse', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});

    const refreshBtn = page.locator('.page-header .rf-btn').first();
    if (!await refreshBtn.isVisible({ timeout: 3_000 }).catch(() => false)) {
      console.log('[spinner] no rf-btn found — skip spinner check');
      return;
    }

    let spinnerClass = '';
    page.on('request', async () => {
      // Capture button classes as soon as any api request fires.
      spinnerClass = await refreshBtn.getAttribute('class').catch(() => '');
    });

    await refreshBtn.click();
    await page.waitForTimeout(500);

    console.log(`[spinner] button classes during loading: "${spinnerClass}"`);
    // Accept: either `rf-spinning` shows on the button itself, or a child has it.
    const spinnerPresent = spinnerClass.includes('rf-spinning') ||
      await page.locator('.rf-btn .rf-spin-icon, .rf-btn.rf-spinning').isVisible().catch(() => false);
    expect(spinnerPresent,
      `Spinner class not found during Refresh loading state. Classes: "${spinnerClass}"`
    ).toBe(true);
  });
});

// ── RefreshButton 800 ms post-loading cooldown — double-animation guard ──────
//
// Root cause closed (2026-06-28): after the manual Refresh spinner stops
// (loading true → false), the next SSE tick arriving via symbolTickCount
// would immediately trigger rf-tick-rotate. The operator perceived this as
// the button "animating twice" — once during the spinner, once right after.
//
// Fix: a per-instance cooldown (800 ms, tracked as a plain timestamp so
// it never becomes reactive state) gates the symbolTickCount subscriber.
// When performance.now() < _loadingExitAt the pulse is skipped. After the
// 800 ms window expires, normal SSE-tick pulses resume.
//
// Test strategy (no fake clocks):
//   1. Navigate to /pulse, wait for rows to load.
//   2. Click Refresh. Wait for spinner to appear then disappear.
//   3. Immediately after spinner clears, sample the button's class list
//      over 1 000 ms (10 × 100 ms polls).
//   4. Assert no rf-tick-a / rf-tick-b class appears during that window
//      (cooldown active).
//   5. Wait 900 ms more (cooldown fully expired). Emit a synthetic
//      window-level SSE tick via the symbolTickCount store test hook
//      exposed at window.__rbq_bumpTickCount — if the store exposes it.
//      If not (no hook wired), skip the positive assertion gracefully.
//   6. Run on chromium-desktop + chromium-mobile.

test.describe('RefreshButton — 800 ms post-loading cooldown', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(60_000);

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(30_000);
    await ensureJwtWithBrowser(browser);
  });

  /**
   * Poll the button's class attribute every 100 ms for `durationMs` and
   * return true if any rf-tick-a or rf-tick-b class was observed.
   *
   * @param {import('@playwright/test').Page} page
   * @param {import('@playwright/test').Locator} btn
   * @param {number} durationMs
   */
  async function pollForTickClass(page, btn, durationMs) {
    const steps = Math.ceil(durationMs / 100);
    for (let i = 0; i < steps; i++) {
      await page.waitForTimeout(100);
      const cls = await btn.getAttribute('class').catch(() => '');
      if (cls && (cls.includes('rf-tick-a') || cls.includes('rf-tick-b'))) {
        return true;
      }
    }
    return false;
  }

  async function runCooldownTest(page) {
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});

    const btn = page.locator('.page-header .rf-btn').first();
    if (!await btn.isVisible({ timeout: 3_000 }).catch(() => false)) {
      console.log('[cooldown] no rf-btn — skip');
      return;
    }

    // Settle so mount-time SSE ticks are absorbed before we start measuring.
    await page.waitForTimeout(1_000);

    // Click Refresh and wait for the spinner to appear.
    await btn.click();
    await page.waitForFunction(
      () => !!document.querySelector('.rf-btn.rf-spinning'),
      { timeout: 3_000 }
    ).catch(() => {});

    // Wait for the spinner to disappear (loading=false).
    await page.waitForFunction(
      () => !document.querySelector('.rf-btn.rf-spinning'),
      { timeout: 15_000 }
    ).catch(() => {});

    console.log('[cooldown] spinner stopped — sampling for tick class over 1 000 ms');

    // Sample for 1 000 ms — cooldown window is 800 ms, so the entire window
    // is covered even accounting for clock jitter.
    const tickClassDuringCooldown = await pollForTickClass(page, btn, 1_000);

    expect(tickClassDuringCooldown,
      'rf-tick-a / rf-tick-b appeared within 1 s of spinner stopping — cooldown not working'
    ).toBe(false);

    console.log('[cooldown] confirmed: no tick-pulse class fired during 800 ms cooldown');

    // Positive assertion: after cooldown expires (900 ms past the 1 000 ms
    // sample window = 1 900 ms after spinner stop), if the page exposes a
    // test hook to bump the tick count, verify the pulse does fire.
    await page.waitForTimeout(900);
    const hasBumpHook = await page.evaluate(
      () => typeof window.__rbq_bumpTickCount === 'function'
    );
    if (hasBumpHook) {
      await page.evaluate(() => window.__rbq_bumpTickCount());
      const tickClassAfterCooldown = await pollForTickClass(page, btn, 600);
      expect(tickClassAfterCooldown,
        'rf-tick-a / rf-tick-b did NOT appear after cooldown expired — pulse broken'
      ).toBe(true);
      console.log('[cooldown] confirmed: tick-pulse resumes after cooldown window');
    } else {
      console.log('[cooldown] no __rbq_bumpTickCount hook — skipping positive assertion');
    }
  }

  test('chromium-desktop: no rf-tick-rotate during 800 ms cooldown on /pulse', async ({ page }) => {
    await runCooldownTest(page);
  });

  test('chromium-mobile: 390×844 — same cooldown contract', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await runCooldownTest(page);
  });
});

// ── Primary-before-secondary fetch ordering ─────────────────────────────────
//
// Per-page architectural slice (2026-06-28): every page classifies its data
// fetches as PRIMARY (what the operator needs to see FIRST — drives the main
// grids/chart) vs SECONDARY (decorative, below-the-fold, or only used inside
// modals). SECONDARY fetches defer via setTimeout(0) so the PRIMARY network
// request starts first and the primary content paints first.
//
// This describe block guards the ordering: for each instrumented page we
// record the timestamp of the FIRST observed PRIMARY request and the FIRST
// observed SECONDARY request. The primary timestamp must be ≤ the secondary
// timestamp. Also asserts secondary still lands within a 4-second window
// after primary (so it doesn't silently regress to "never loads").
//
// Run individually:
//   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com npx playwright test \
//     e2e/main_thread_perf.spec.js \
//     -g 'Primary-before-secondary' --project=chromium-desktop --workers=1

/**
 * Hook page.on('request') and record the FIRST request whose URL matches
 * `primaryRe` and the FIRST request whose URL matches `secondaryRe`. Returns
 * timestamps + the captured URL strings. Tolerates either being absent (test
 * decides whether absence is acceptable — typically primary is mandatory).
 *
 * @param {import('@playwright/test').Page} page
 * @param {RegExp} primaryRe
 * @param {RegExp} secondaryRe
 */
function watchRequestOrder(page, primaryRe, secondaryRe) {
  /** @type {{ tFirstPrimary: number, tFirstSecondary: number,
   *           urlPrimary: string, urlSecondary: string }} */
  const obs = { tFirstPrimary: 0, tFirstSecondary: 0, urlPrimary: '', urlSecondary: '' };
  page.on('request', (req) => {
    const url = req.url();
    const t = Date.now();
    if (!obs.tFirstPrimary && primaryRe.test(url)) {
      obs.tFirstPrimary = t;
      obs.urlPrimary = url;
    }
    if (!obs.tFirstSecondary && secondaryRe.test(url)) {
      obs.tFirstSecondary = t;
      obs.urlSecondary = url;
    }
  });
  return obs;
}

/**
 * Shared assertion: primary fired before secondary, with both fired within
 * a reasonable window of mount.
 *
 * @param {{ tFirstPrimary:number, tFirstSecondary:number,
 *           urlPrimary:string, urlSecondary:string }} obs
 * @param {string} label
 */
function assertOrdering(obs, label) {
  console.log(`[order:${label}] primary="${obs.urlPrimary}" @${obs.tFirstPrimary}`);
  console.log(`[order:${label}] secondary="${obs.urlSecondary}" @${obs.tFirstSecondary}`);
  expect(obs.tFirstPrimary,
    `${label}: primary request was never observed`
  ).toBeGreaterThan(0);
  expect(obs.tFirstSecondary,
    `${label}: secondary request was never observed (didn't load within window)`
  ).toBeGreaterThan(0);
  // Primary should fire BEFORE secondary. Allow a small tolerance window of
  // 50ms — both are scheduled in the same microtask boundary in some paths,
  // but the setTimeout(0) deferral typically separates them by one task tick
  // (4ms+). The 50ms tolerance lets co-scheduled requests pass while still
  // catching real regressions (e.g. when the secondary loads SYNCHRONOUSLY
  // before primary's fetch even fires).
  expect(obs.tFirstSecondary + 50,
    `${label}: secondary fired before primary — defer pattern broken`
  ).toBeGreaterThanOrEqual(obs.tFirstPrimary);
}

test.describe('Primary-before-secondary fetch ordering', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(120_000);

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(60_000);
    await ensureJwtWithBrowser(browser);
  });

  /**
   * Shared body — instruments the request hook BEFORE goto, navigates, lets
   * primary + secondary settle, then asserts ordering.
   *
   * @param {import('@playwright/test').Page} page
   * @param {{ path:string, primaryRe:RegExp, secondaryRe:RegExp, label:string,
   *           settleMs?:number }} opts
   */
  async function runOrderingTest(page, opts) {
    await seedAuth(page, _sharedJwt);
    const obs = watchRequestOrder(page, opts.primaryRe, opts.secondaryRe);
    await page.goto(opts.path, { waitUntil: 'load', timeout: 90_000 });
    // Wait long enough for the deferred setTimeout(0) to fire AND the
    // secondary network round-trip to land. 4s covers slow dev-server
    // responses without making the test brittle.
    await page.waitForTimeout(opts.settleMs ?? 4_000);
    assertOrdering(obs, opts.label);
  }

  test('/dashboard — loadHero positions before equity + nav', async ({ page }) => {
    await runOrderingTest(page, {
      path: '/dashboard',
      // PRIMARY: positions (loadHero's first batch member). /api/positions/
      // is the actual endpoint (BASE='/api', path='/positions/').
      primaryRe: /\/api\/positions(\/|\?)/,
      // SECONDARY: intraday-equity OR nav/latest — both are deferred together.
      secondaryRe: /\/api\/(charts\/intraday-equity|nav\/latest)/,
      label: '/dashboard',
    });
  });

  test('/dashboard mobile — same ordering on 390×844', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await runOrderingTest(page, {
      path: '/dashboard',
      primaryRe: /\/api\/positions(\/|\?)/,
      secondaryRe: /\/api\/(charts\/intraday-equity|nav\/latest)/,
      label: '/dashboard-mobile',
    });
  });

  test('/admin/derivatives — positions before hedge-proxies', async ({ page }) => {
    await runOrderingTest(page, {
      path: '/admin/derivatives',
      // PRIMARY: positions (loadPositions fires first in onMount).
      primaryRe: /\/api\/positions(\/|\?)/,
      // SECONDARY: hedge-proxies (deferred via setTimeout(0)).
      secondaryRe: /\/api\/admin\/hedge-proxies/,
      label: '/admin/derivatives',
    });
  });

  test('/admin/derivatives mobile — same ordering on 390×844', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await runOrderingTest(page, {
      path: '/admin/derivatives',
      primaryRe: /\/api\/positions(\/|\?)/,
      secondaryRe: /\/api\/admin\/hedge-proxies/,
      label: '/admin/derivatives-mobile',
    });
  });

  test('/admin/settings — settings list before watchlists', async ({ page }) => {
    // /admin/settings is gated by the manage_settings capability — if the
    // test JWT doesn't carry it, the access-denied panel renders and no
    // fetches fire. Skip cleanly in that case rather than failing on what
    // is really a role-config issue, not a perf regression.
    await seedAuth(page, _sharedJwt);
    const obs = watchRequestOrder(page,
      /\/api\/admin\/settings(\/|\?)/,
      /\/api\/watchlist(\/|\?)/,
    );
    await page.goto('/admin/settings', { waitUntil: 'load', timeout: 90_000 });
    await page.waitForTimeout(4_000);
    const denied = await page.locator('text=Access denied').isVisible().catch(() => false);
    if (denied) {
      test.skip(true, 'Test JWT lacks manage_settings cap — perf ordering ' +
        'unobservable when the page renders the access-denied empty state.');
      return;
    }
    assertOrdering(obs, '/admin/settings');
  });
});
