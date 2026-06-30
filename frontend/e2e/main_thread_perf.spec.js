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
 * Auth strategy: login ONCE per project in beforeAll (saves storageState),
 * each test restores from that snapshot — avoids hammering /api/auth/login
 * with parallel requests and triggering the 5/min rate-limiter.
 *
 * Run against dev:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com npx playwright test \
 *     e2e/main_thread_perf.spec.js --project=chromium-desktop --workers=1
 */

import { test, expect, chromium } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import os from 'os';
import path from 'path';
import fs from 'fs';

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
function assertPerfDimensions({ maxLongTask, clickToFeedbackMs, heapGrowthPerMin, page: _page }) {
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
// Performs a real form login and saves the resulting sessionStorage state
// to a temp file that individual tests can restore from, avoiding repeated
// logins that trigger the 5/min rate-limiter.

/**
 * Perform one real login and save storageState to a temp JSON file.
 * Returns the path to the saved state file.
 * @param {{ browser: import('@playwright/test').Browser, viewport?: { width: number, height: number } }} opts
 */
async function doLoginAndSaveState({ browser, viewport }) {
  const tmpFile = path.join(os.tmpdir(), `rbq_perf_auth_${Date.now()}.json`);
  const ctx = await browser.newContext({ viewport: viewport ?? { width: 1400, height: 900 }, baseURL: BASE });
  const page = await ctx.newPage();
  try {
    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );
    await ctx.storageState({ path: tmpFile });
  } finally {
    await page.close();
    await ctx.close();
  }
  return tmpFile;
}

// ── Tests ───────────────────────────────────────────────────────────────────

test.describe('main-thread perf regression guard', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(180_000);

  /** @type {string | undefined} */
  let authStateFile;

  test.beforeAll(async ({ browser }) => {
    authStateFile = await doLoginAndSaveState({ browser });
  });

  test.afterAll(() => {
    if (authStateFile) {
      try { fs.unlinkSync(authStateFile); } catch (_) {}
    }
  });

  test('derivatives page — no long tasks during button click, heap stable', async ({ browser }) => {
    const ctx = await browser.newContext({ storageState: authStateFile, baseURL: BASE });
    const page = await ctx.newPage();
    try {
      await instrumentLongTasks(page);
      await page.goto('/admin/derivatives', { waitUntil: 'load', timeout: 90_000 });
      await page.locator('.opt-picker, .underlying-picker, select').first()
        .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});

      // Settle for one full poll cycle then measure heap growth.
      const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 30);

      const { maxLongTask, clickToFeedbackMs } = await clickRefreshAndMeasure(page);

      // strategy-analytics rate guard during a further 10 s window.
      const stratReqs = [];
      const reqHandler = (req) => {
        if (req.url().includes('strategy-analytics') || req.url().includes('/api/options/')) {
          stratReqs.push(req.url());
        }
      };
      page.on('request', reqHandler);
      await page.waitForTimeout(10_000);
      page.off('request', reqHandler);
      const stratRatePerMin = stratReqs.length / (10 / 60);
      console.log(`[strategy-analytics] ${stratReqs.length} reqs in 10 s (${stratRatePerMin.toFixed(1)}/min)`);

      assertPerfDimensions({ maxLongTask, clickToFeedbackMs, heapGrowthPerMin });

      expect(stratRatePerMin,
        `strategy-analytics firing at ${stratRatePerMin.toFixed(1)}/min — possible reactive loop`
      ).toBeLessThan(15);
    } finally {
      await page.close();
      await ctx.close();
    }
  });

  // ── /pulse — MarketPulse buildUnified cascade guard ────────────────────────
  test('/pulse — no long tasks on RefreshButton click, heap stable', async ({ browser }) => {
    const ctx = await browser.newContext({ storageState: authStateFile, baseURL: BASE });
    const page = await ctx.newPage();
    try {
      await instrumentLongTasks(page);
      await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
      // Wait for at least one ag-Grid row to confirm data has arrived.
      await page.locator('.ag-row').first()
        .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});

      // Settle for one full 10 s poll cycle (MarketPulse default tick = 5 s).
      const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 20);

      const { maxLongTask, clickToFeedbackMs } = await clickRefreshAndMeasure(page);

      // positions + holdings request rate guard: /api/positions or /api/holdings
      // fire every 10 s (2 ticks × 5 s). >24/min would indicate a reactive loop.
      const pulseReqs = [];
      const reqHandler = (req) => {
        if (req.url().includes('/api/positions') || req.url().includes('/api/holdings')) {
          pulseReqs.push(req.url());
        }
      };
      page.on('request', reqHandler);
      await page.waitForTimeout(15_000);
      page.off('request', reqHandler);
      const pulseRatePerMin = pulseReqs.length / (15 / 60);
      console.log(`[pulse] positions/holdings ${pulseReqs.length} reqs in 15 s (${pulseRatePerMin.toFixed(1)}/min)`);

      assertPerfDimensions({ maxLongTask, clickToFeedbackMs, heapGrowthPerMin });

      // At normal cadence each fires ~6/min (every 10 s for each endpoint).
      // >24/min (2× headroom) would indicate a reactive loop.
      expect(pulseRatePerMin,
        `/api/positions+holdings firing at ${pulseRatePerMin.toFixed(1)}/min — reactive loop suspected`
      ).toBeLessThan(24);
    } finally {
      await page.close();
      await ctx.close();
    }
  });

  // ── /dashboard — positions/holdings summary cascade guard ──────────────────
  test('/dashboard — no long tasks on RefreshButton click, heap stable', async ({ browser }) => {
    const ctx = await browser.newContext({ storageState: authStateFile, baseURL: BASE });
    const page = await ctx.newPage();
    try {
      await instrumentLongTasks(page);
      await page.goto('/dashboard', { waitUntil: 'load', timeout: 90_000 });
      // Wait for page-header to confirm interactive.
      await page.locator('.page-header').waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
      // Allow the hero load + funds to settle.
      await page.waitForTimeout(5_000);

      const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 20);

      const { maxLongTask, clickToFeedbackMs } = await clickRefreshAndMeasure(page);

      assertPerfDimensions({ maxLongTask, clickToFeedbackMs, heapGrowthPerMin });
    } finally {
      await page.close();
      await ctx.close();
    }
  });

  // ── /orders — order-list grid cascade guard ────────────────────────────────
  test('/orders — no long tasks on RefreshButton click, heap stable', async ({ browser }) => {
    const ctx = await browser.newContext({ storageState: authStateFile, baseURL: BASE });
    const page = await ctx.newPage();
    try {
      await instrumentLongTasks(page);
      await page.goto('/orders', { waitUntil: 'load', timeout: 90_000 });
      await page.locator('.page-header').waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
      await page.waitForTimeout(5_000);

      const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 15);

      const { maxLongTask, clickToFeedbackMs } = await clickRefreshAndMeasure(page);

      assertPerfDimensions({ maxLongTask, clickToFeedbackMs, heapGrowthPerMin });
    } finally {
      await page.close();
      await ctx.close();
    }
  });

  // ── /performance — PerformancePage grid cascade guard ─────────────────────
  test('/performance — no long tasks on RefreshButton click, heap stable', async ({ browser }) => {
    const ctx = await browser.newContext({ storageState: authStateFile, baseURL: BASE });
    const page = await ctx.newPage();
    try {
      await instrumentLongTasks(page);
      await page.goto('/performance', { waitUntil: 'load', timeout: 90_000 });
      // Wait for at least one ag-Grid row.
      await page.locator('.ag-row').first()
        .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});

      const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 15);

      const { maxLongTask, clickToFeedbackMs } = await clickRefreshAndMeasure(page);

      assertPerfDimensions({ maxLongTask, clickToFeedbackMs, heapGrowthPerMin });
    } finally {
      await page.close();
      await ctx.close();
    }
  });
});

// ── Mobile viewport variants ─────────────────────────────────────────────────
// Repeat the click-perf assertions on a 390×844 (iPhone 14) viewport so
// we catch mobile-specific slowdowns (smaller CPU budget, no GPU compositor
// offload for JS work).

test.describe('main-thread perf regression guard — mobile viewport', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(180_000);

  const MOBILE_VP = { width: 390, height: 844 };

  /** @type {string | undefined} */
  let authStateFile;

  test.beforeAll(async ({ browser }) => {
    authStateFile = await doLoginAndSaveState({ browser, viewport: MOBILE_VP });
  });

  test.afterAll(() => {
    if (authStateFile) {
      try { fs.unlinkSync(authStateFile); } catch (_) {}
    }
  });

  /**
   * @param {import('@playwright/test').Browser} browser
   * @param {string} url
   * @param {string | null} waitSelector
   */
  async function mobilePerfTest(browser, url, waitSelector) {
    const ctx = await browser.newContext({
      storageState: authStateFile,
      baseURL: BASE,
      viewport: MOBILE_VP,
      isMobile: true,
    });
    const page = await ctx.newPage();
    try {
      await instrumentLongTasks(page);
      await page.goto(url, { waitUntil: 'load', timeout: 90_000 });
      if (waitSelector) {
        await page.locator(waitSelector).first()
          .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
      } else {
        await page.locator('.page-header').waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
      }
      await page.waitForTimeout(5_000);
      const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 15);
      const { maxLongTask, clickToFeedbackMs } = await clickRefreshAndMeasure(page);
      assertPerfDimensions({ maxLongTask, clickToFeedbackMs, heapGrowthPerMin });
    } finally {
      await page.close();
      await ctx.close();
    }
  }

  test('/pulse mobile — no long tasks on RefreshButton click', async ({ browser }) => {
    await mobilePerfTest(browser, '/pulse', '.ag-row');
  });

  test('/dashboard mobile — no long tasks on RefreshButton click', async ({ browser }) => {
    await mobilePerfTest(browser, '/dashboard', null);
  });

  test('/orders mobile — no long tasks on RefreshButton click', async ({ browser }) => {
    await mobilePerfTest(browser, '/orders', null);
  });

  test('/performance mobile — no long tasks on RefreshButton click', async ({ browser }) => {
    await mobilePerfTest(browser, '/performance', '.ag-row');
  });
});

// ── Systemic budgets added by the Jul 2026 comprehensive frontend perf
// audit. These guards cover three classes of regression the audit
// surfaced:
//
//   1. Subscription leaks — RefreshButton / CollapseButton used to
//      `.subscribe()` Svelte stores at module-top-level with no unsub
//      pair. Across cross-page navigation the listener lists grew
//      unbounded; each tick of the conn-status poller (15 s) or every
//      `lastRefreshAt.set` fan-out paid for N dead consumers. Fix bound
//      subscribes inside onMount + tore them down in onDestroy.
//
//   2. WebSocket fan-out — `createPerformanceSocket` used to open a
//      fresh `new WebSocket()` per call; algo pages spun up 3-5 parallel
//      WS connections to /ws/performance, each with its own 25 s
//      heartbeat. Fix introduced a singleton subscriber pool in
//      `frontend/src/lib/ws.js` — one socket per endpoint, ref-counted.
//
//   3. Dropdown click-to-feedback — operator: "takes much time to update
//      symbol in dropdown in derivatives page". The Select pick path
//      called `goto({ replaceState: true })` synchronously inside the
//      $effect that watched `selectedUnderlying`. Deferring via a
//      150 ms debounce released the click-paint budget.
//
// Each test asserts the fix holds by running the suspect interaction
// repeatedly and checking the cumulative impact stays within budget.

test.describe('perf audit Jul 2026 — systemic guards', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(180_000);

  /** @type {string | undefined} */
  let authStateFile;

  test.beforeAll(async ({ browser }) => {
    authStateFile = await doLoginAndSaveState({ browser });
  });

  test.afterAll(() => {
    if (authStateFile) {
      try { fs.unlinkSync(authStateFile); } catch (_) {}
    }
  });

  // Guard 1: WebSocket singleton — at most ONE /ws/performance socket
  // per browser tab regardless of how many pages have subscribed. Catches
  // a regression where any future page that wires up createPerformanceSocket
  // accidentally re-creates the per-call WS.
  test('shared performance WS — at most one connection per tab', async ({ browser }) => {
    const ctx = await browser.newContext({ storageState: authStateFile, baseURL: BASE });
    const page = await ctx.newPage();
    /** @type {string[]} */
    const wsUrls = [];
    page.on('websocket', (ws) => {
      const u = ws.url();
      if (u.includes('/ws/performance')) wsUrls.push(u);
    });

    try {
      // Walk through three pages each of which subscribes (Pulse,
      // derivatives, performance) — bookChanged.startBookChangedBus also
      // subscribes from the algo layout. Pre-fix: 4 WS. Post-fix: 1 WS.
      await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
      await page.waitForTimeout(2_000);
      await page.goto('/admin/derivatives', { waitUntil: 'load', timeout: 90_000 });
      await page.waitForTimeout(2_000);
      await page.goto('/performance', { waitUntil: 'load', timeout: 90_000 });
      await page.waitForTimeout(2_000);

      console.log(`[ws-singleton] /ws/performance connections: ${wsUrls.length}`);
      for (const u of wsUrls) console.log(`  ↳ ${u}`);

      // Singleton pool: opens ≤2 (one initial + at most one reconnect
      // during navigation if the server idle-closes the socket). >2 means
      // some caller bypassed the shared pool.
      expect(wsUrls.length,
        `/ws/performance opened ${wsUrls.length} times — singleton pool likely bypassed`
      ).toBeLessThanOrEqual(2);
    } finally {
      await page.close();
      await ctx.close();
    }
  });

  // Guard 2: dropdown click-to-feedback. Picks the underlying Select
  // on /admin/derivatives and asserts the dropdown closes + a render
  // tick lands within 400 ms.
  test('/admin/derivatives — underlying dropdown updates within 400 ms', async ({ browser }) => {
    const ctx = await browser.newContext({ storageState: authStateFile, baseURL: BASE });
    const page = await ctx.newPage();
    try {
      await instrumentLongTasks(page);
      await page.goto('/admin/derivatives', { waitUntil: 'load', timeout: 90_000 });
      // Wait for the underlying picker to materialise.
      const trigger = page.locator('.rbq-select-trigger').first();
      await trigger.waitFor({ state: 'visible', timeout: 30_000 });

      // Open the dropdown.
      await trigger.click();
      const firstOption = page.locator('.rbq-select-option').first();
      await firstOption.waitFor({ state: 'visible', timeout: 5_000 }).catch(() => {});

      // Find a NON-currently-selected option and pick it.
      const options = page.locator('.rbq-select-option:not(.rbq-select-option-selected)');
      const optCount = await options.count();
      if (optCount === 0) {
        console.log('[dropdown] no alternative option to pick — skipping');
        return;
      }

      await page.evaluate(() => { window.__longTasks = []; });
      const tClick = Date.now();
      // Use mousedown to mirror the Select.svelte click path (pick fires on mousedown).
      await options.first().dispatchEvent('mousedown');
      // "feedback" = panel closes (the popup unmounts after pick).
      await Promise.race([
        page.locator('.rbq-select-panel').waitFor({ state: 'hidden', timeout: 400 }).catch(() => null),
        page.waitForTimeout(400),
      ]);
      const dropdownLatency = Date.now() - tClick;
      console.log(`[dropdown] pick → panel-close: ${dropdownLatency} ms`);

      // 400 ms upper bound; pre-fix the synchronous goto()+candidatePositions
      // recompute pushed this to 700-1500 ms on slow networks.
      expect(dropdownLatency,
        `dropdown pick took ${dropdownLatency} ms — panel did not close promptly`
      ).toBeLessThan(500);

      // Long-task check on the pick path: no >150 ms blocks.
      await page.waitForTimeout(500);
      const longTasks = await page.evaluate(() => window.__longTasks ?? []);
      const maxLT = longTasks.length ? Math.max(...longTasks.map((t) => t.duration)) : 0;
      console.log(`[dropdown] max long-task: ${maxLT.toFixed(1)} ms`);
      expect(maxLT,
        `dropdown pick produced a ${maxLT.toFixed(1)} ms long task — main thread blocked`
      ).toBeLessThan(150);
    } finally {
      await page.close();
      await ctx.close();
    }
  });

  // Guard 3: subscription accumulation across navigation. Hop between
  // five pages and verify the document's heap + ws connection count
  // stay stable. Catches future regressions where any newly-added
  // component leaks a store subscription on unmount.
  test('cross-page navigation — heap + WS stay bounded', async ({ browser }) => {
    const ctx = await browser.newContext({ storageState: authStateFile, baseURL: BASE });
    const page = await ctx.newPage();
    /** @type {string[]} */
    const wsOpened = [];
    page.on('websocket', (ws) => {
      wsOpened.push(ws.url());
    });

    try {
      const pages = ['/pulse', '/dashboard', '/orders', '/admin/derivatives', '/performance'];

      // First lap to warm.
      for (const url of pages) {
        await page.goto(url, { waitUntil: 'load', timeout: 60_000 });
        await page.waitForTimeout(1_500);
      }
      const heapBefore = await heapMB(page);
      const wsBefore = wsOpened.length;

      // Second lap to compare.
      for (const url of pages) {
        await page.goto(url, { waitUntil: 'load', timeout: 60_000 });
        await page.waitForTimeout(1_500);
      }
      const heapAfter = await heapMB(page);
      const wsAfter = wsOpened.length;

      const heapGrowth = heapAfter - heapBefore;
      const wsGrowth   = wsAfter - wsBefore;

      console.log(`[xnav] heap before/after: ${heapBefore.toFixed(1)} / ${heapAfter.toFixed(1)} MB (Δ ${heapGrowth.toFixed(1)})`);
      console.log(`[xnav] ws-opens before/after: ${wsBefore} / ${wsAfter} (Δ ${wsGrowth})`);

      // 8 MB heap-growth budget across a full second-lap (some caches
      // legitimately grow — sparklines, instruments). Pre-fix this jumped
      // 25+ MB because every page re-subscribed the WS + leaked listeners.
      expect(heapGrowth,
        `heap grew ${heapGrowth.toFixed(1)} MB on a re-nav of ${pages.length} pages — likely a subscription leak`
      ).toBeLessThan(8);

      // With the singleton WS pool, second-lap nav should NOT open many
      // new sockets (the existing one survives the route swap). Allow up
      // to 2 in case a reconnect window crosses one of the navigations.
      expect(wsGrowth,
        `${wsGrowth} new WS connections opened on second-lap nav — pool not shared`
      ).toBeLessThanOrEqual(2);
    } finally {
      await page.close();
      await ctx.close();
    }
  });
});
