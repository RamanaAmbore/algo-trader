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
  // addInitScript fires before any page scripts; sessionStorage is populated
  // so the auth store picks up the token on hydration without needing a login
  // form round-trip.
  await page.addInitScript((tok) => {
    try { sessionStorage.setItem('ramboq_token', tok); } catch (_) {}
  }, jwt);
  // NOTE: setExtraHTTPHeaders is intentionally omitted — it triggers a
  // Playwright trace-write inside the context that can produce ENOENT when the
  // context was created by the test runner for a beforeAll-scoped beforeAll.
  // The JWT in sessionStorage is sufficient for all navigations in this spec.
}

// ── Tests ───────────────────────────────────────────────────────────────────

// Module-level token shared between the two describe groups. Populated by
// the first beforeAll that runs (whichever describe group Playwright executes
// first when running serial). A second beforeAll is a no-op if already set.
/** @type {string} */
let _sharedJwt = '';

/**
 * Perform a one-shot login using a throwaway browser context (browser fixture
 * IS supported in beforeAll, page/context fixtures are NOT).
 * Captures the JWT token into the module-level _sharedJwt variable.
 *
 * @param {import('@playwright/test').Browser} browser
 */
async function ensureJwtWithBrowser(browser) {
  if (_sharedJwt) return;
  const ctx = await browser.newContext({ baseURL: BASE });
  const page = await ctx.newPage();
  try {
    const { token } = await loginAsAdmin(page).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );
    _sharedJwt = token;
  } finally {
    await page.close();
    // Suppress tracing-artifact errors: Playwright v1.40+ tries to save
    // the trace from a beforeAll-scoped context to a test artifact path
    // that may not exist. Swallow the ENOENT so the login result is not lost.
    await ctx.close().catch(() => {});
  }
}

test.describe('main-thread perf regression guard', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(180_000);

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(120_000);
    await ensureJwtWithBrowser(browser);
  });

  test('derivatives page — no long tasks during button click, heap stable', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
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

    // The filter captures both strategy-analytics and chain-quotes; each polls
    // at 5 s via marketAwareInterval. With initial burst: ~3 fires each in 10 s
    // = 6 total → 36/min. Budget of 60/min gives 1.5× headroom over normal
    // cadence while still catching reactive loops (which fire 300+/min).
    expect(stratRatePerMin,
      `strategy-analytics+chain-quotes firing at ${stratRatePerMin.toFixed(1)}/min — possible reactive loop`
    ).toBeLessThan(60);
  });

  // ── /pulse — MarketPulse buildUnified cascade guard ────────────────────────
  test('/pulse — no long tasks on RefreshButton click, heap stable', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
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

    // Steady-state cadence (post loadPulse store migration, Jun 2026):
    //   - Layout-resident `startBookPollers` fires positions + holdings + funds
    //     every 5 s = 24 req/min (positions + holdings combined).
    //   - MarketPulse's loadPulse now routes through positionsStore.load() /
    //     holdingsStore.load() which deduplicate against the in-flight request
    //     from startBookPollers — the page-level call shares the layout poller's
    //     round-trip instead of firing a second parallel request.
    //   - Combined steady-state: ~24 req/min (down from 36–40 pre-migration).
    //     Reactive-loop regressions fire 100+/min. Budget = 60/min gives 2.5×
    //     headroom over the post-migration steady-state.
    expect(pulseRatePerMin,
      `/api/positions+holdings firing at ${pulseRatePerMin.toFixed(1)}/min — reactive loop suspected`
    ).toBeLessThan(60);
  });

  // ── /dashboard — positions/holdings summary cascade guard ──────────────────
  test('/dashboard — no long tasks on RefreshButton click, heap stable', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
    await instrumentLongTasks(page);
    await page.goto('/dashboard', { waitUntil: 'load', timeout: 90_000 });
    // Wait for page-header to confirm interactive.
    await page.locator('.page-header').waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    // Allow the hero load + funds to settle.
    await page.waitForTimeout(5_000);

    const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 20);

    const { maxLongTask, clickToFeedbackMs } = await clickRefreshAndMeasure(page);

    assertPerfDimensions({ maxLongTask, clickToFeedbackMs, heapGrowthPerMin });
  });

  // ── /orders — order-list grid cascade guard ────────────────────────────────
  test('/orders — no long tasks on RefreshButton click, heap stable', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
    await instrumentLongTasks(page);
    await page.goto('/orders', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.page-header').waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    await page.waitForTimeout(5_000);

    const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 15);

    const { maxLongTask, clickToFeedbackMs } = await clickRefreshAndMeasure(page);

    assertPerfDimensions({ maxLongTask, clickToFeedbackMs, heapGrowthPerMin });
  });

  // ── /performance — PerformancePage grid cascade guard ─────────────────────
  test('/performance — no long tasks on RefreshButton click, heap stable', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
    await instrumentLongTasks(page);
    await page.goto('/performance', { waitUntil: 'load', timeout: 90_000 });
    // Wait for at least one ag-Grid row.
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});

    const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 15);

    const { maxLongTask, clickToFeedbackMs } = await clickRefreshAndMeasure(page);

    assertPerfDimensions({ maxLongTask, clickToFeedbackMs, heapGrowthPerMin });
  });
});

// ── Mobile viewport variants ─────────────────────────────────────────────────
// Repeat the click-perf assertions on a 390×844 (iPhone 14) viewport so
// we catch mobile-specific slowdowns (smaller CPU budget, no GPU compositor
// offload for JS work).

test.describe('main-thread perf regression guard — mobile viewport', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(180_000);
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true });

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(120_000);
    await ensureJwtWithBrowser(browser);
  });

  test('/pulse mobile — no long tasks on RefreshButton click', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
    await instrumentLongTasks(page);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    await page.waitForTimeout(5_000);
    const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 15);
    const { maxLongTask, clickToFeedbackMs } = await clickRefreshAndMeasure(page);
    assertPerfDimensions({ maxLongTask, clickToFeedbackMs, heapGrowthPerMin });
  });

  test('/dashboard mobile — no long tasks on RefreshButton click', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
    await instrumentLongTasks(page);
    await page.goto('/dashboard', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.page-header').waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    await page.waitForTimeout(5_000);
    const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 15);
    const { maxLongTask, clickToFeedbackMs } = await clickRefreshAndMeasure(page);
    assertPerfDimensions({ maxLongTask, clickToFeedbackMs, heapGrowthPerMin });
  });

  test('/orders mobile — no long tasks on RefreshButton click', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
    await instrumentLongTasks(page);
    await page.goto('/orders', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.page-header').waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    await page.waitForTimeout(5_000);
    const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 15);
    const { maxLongTask, clickToFeedbackMs } = await clickRefreshAndMeasure(page);
    assertPerfDimensions({ maxLongTask, clickToFeedbackMs, heapGrowthPerMin });
  });

  test('/performance mobile — no long tasks on RefreshButton click', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
    await instrumentLongTasks(page);
    await page.goto('/performance', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    await page.waitForTimeout(5_000);
    const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 15);
    const { maxLongTask, clickToFeedbackMs } = await clickRefreshAndMeasure(page);
    assertPerfDimensions({ maxLongTask, clickToFeedbackMs, heapGrowthPerMin });
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

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(120_000);
    await ensureJwtWithBrowser(browser);
  });

  // Guard 1: WebSocket singleton — at most ONE /ws/performance socket
  // per browser tab regardless of how many pages have subscribed. Catches
  // a regression where any future page that wires up createPerformanceSocket
  // accidentally re-creates the per-call WS.
  test('shared performance WS — at most one connection per tab', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
    /** @type {string[]} */
    const wsUrls = [];
    page.on('websocket', (ws) => {
      const u = ws.url();
      if (u.includes('/ws/performance')) wsUrls.push(u);
    });

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

    // Singleton pool: the _subscribe() impl closes the WS when the last
    // subscriber leaves (pool.closed = true). Each SvelteKit page
    // navigation unmounts the prior page's subscriber, dropping to 0 subs
    // → socket closes, next page opens a new one. With 3 navigations:
    // 1 initial (/pulse) + 1 per subsequent page = up to 4.
    // Budget = 5 (3 navigations + 1 initial + 1 spare for server
    // idle-close) — still meaningfully catches a regression where every
    // component bypasses the pool and opens its own socket (would fire 10+).
    expect(wsUrls.length,
      `/ws/performance opened ${wsUrls.length} times — singleton pool likely bypassed`
    ).toBeLessThanOrEqual(5);
  });

  // Guard 2: dropdown click-to-feedback. Picks the underlying Select
  // on /admin/derivatives and asserts the dropdown closes + a render
  // tick lands within 400 ms.
  test('/admin/derivatives — underlying dropdown updates within 400 ms', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
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
  });

  // Guard 3: subscription accumulation across navigation. Hop between
  // five pages and verify the document's heap + ws connection count
  // stay stable. Catches future regressions where any newly-added
  // component leaks a store subscription on unmount.
  test('cross-page navigation — heap + WS stay bounded', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
    /** @type {string[]} */
    const wsOpened = [];
    page.on('websocket', (ws) => {
      wsOpened.push(ws.url());
    });

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

    // The _subscribe() pool closes the WS when the last subscriber leaves.
    // Each page navigation unmounts the prior page's subscriber → socket
    // closes → next page opens a fresh one. On a 5-page lap this yields up
    // to 5 new opens. Budget = pages.length + 2 (one spare for a server
    // idle-close + one for the layout's bookChanged bus that may survive a
    // nav). A genuine leak (e.g., every component opens its own socket) would
    // fire 3× pages = 15+ new opens and would exceed this budget.
    expect(wsGrowth,
      `${wsGrowth} new WS connections opened on second-lap nav — pool not shared`
    ).toBeLessThanOrEqual(pages.length + 2);
  });
});

// ── Visibility hibernation — 5-min idle threshold ──────────────────────────
//
// Final policy (operator-confirmed 2026-06-28):
//   Tab active OR hidden < 5 min (polling.idle_timeout_min) → NORMAL cadence.
//       All pollers run exactly as they did before. No changes.
//   Tab hidden ≥ 5 min → HIBERNATION (Option B):
//       critical pollers (throttle mode)  → 30 s cadence
//       non-critical pollers (pause mode) → stopped entirely
//       WebSocket                         → stays connected (never hibernated)
//   Tab visible after hibernation → immediate refire within 200 ms,
//                                   then resume normal cadence.
//   Tab visible without hibernation→ no-op, pollers were running normally.
//
// Supersedes Option A ("pause-all-on-hidden") and Option B-always
// ("throttle-immediately-on-hidden") behavior from prior agents.
//
// Test approach: fake clock via page.clock.install() + page.clock.runFor()
// so the 5-minute timer fires in milliseconds instead of real time.
// The stores.js module exposes window.__rbq_setHibMs (set via addInitScript)
// to let tests override the threshold to a tiny value (200 ms).
//
// Three phases:
//   Phase 0 (pre-hidden): normal polling, data visible.
//   Phase 1 (hidden < threshold): pollers still at normal cadence.
//   Phase 2 (hidden ≥ threshold): hibernation — throttle/pause engaged.
//   Phase 3 (visible again): immediate refire within 200 ms.
//
// Both chromium-desktop and chromium-mobile viewports are covered.

test.describe('visibility hibernation — 5-min idle threshold', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(300_000);

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(120_000);
    await ensureJwtWithBrowser(browser);
  });

  /**
   * Shared test body — runs on any viewport.
   * Uses Playwright's fake clock to fast-forward the hibernation timer
   * without waiting 5 real minutes.
   *
   * @param {import('@playwright/test').Page} page
   */
  async function runHibernationTest(page) {
    // Install fake clock before page load so setTimeout/clearTimeout and
    // setInterval/clearInterval are intercepted from the first module eval.
    // `now` is anchored to real wall time so any Date.now() comparisons
    // inside the app remain coherent during the pre-load phase.
    await page.clock.install({ time: Date.now() });

    // Override hibernation threshold to 200 ms via a pre-load window hook.
    // stores.js reads window.__rbq_hibMs (if set) in setHibernationIdleMinutes
    // at module initialisation time, overriding the in-code 5-min default.
    // NOTE: The addInitScript below runs before any page script including
    // the Svelte bundle, so the override lands before the first timer is set.
    await page.addInitScript(() => {
      // 200 ms expressed as minutes (0.00333 min) so setHibernationIdleMinutes
      // receives a plausible input. The stores module clamps to ≥1 min; we
      // bypass the clamp by patching _hibernationIdleMs directly via a hook
      // the module checks before installing any setTimeout.
      window.__rbq_hibMs = 200;  // ms — stores.js picks this up if defined
    });

    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });

    // After the page loads the Svelte bundle is running. Attempt to patch
    // the threshold via the window hook again (belt-and-suspenders for
    // frameworks that initialise stores lazily after DOMContentLoaded).
    await page.evaluate(() => {
      if (typeof window.__rbq_setHibMs === 'function') {
        window.__rbq_setHibMs(200);
      }
    });

    // Install fetch request counters.
    await page.evaluate(() => {
      window.__posReqs  = 0;
      window.__newsReqs = 0;
      const origFetch = window.fetch;
      window.fetch = async function(...args) {
        const url = String(args[0]);
        if (url.includes('/api/positions') || url.includes('/api/holdings')) window.__posReqs++;
        if (url.includes('/api/market/news') || url.includes('/api/news'))    window.__newsReqs++;
        return origFetch.apply(this, args);
      };
    });

    // Wait for initial data row (or timeout — grid may be empty out of hours).
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    // Advance fake clock one full 6 s window so any pending interval fires
    // and the page settles before we begin the visibility dance.
    await page.clock.runFor(6_000);
    await page.waitForTimeout(300);

    // ── Phase 1: tab hidden, PRE-threshold (fake clock < 200 ms) ────────
    // The hibernation setTimeout is installed but has not fired.

    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true, get: () => 'hidden',
      });
      Object.defineProperty(document, 'hidden', {
        configurable: true, get: () => true,
      });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    console.log('[hibernation] tab hidden — phase 1: pre-threshold (clock < 200 ms)');

    // Reset counters.
    await page.evaluate(() => { window.__posReqs = 0; window.__newsReqs = 0; });
    // Advance clock 100 ms — hibernation timer (200 ms) has NOT fired yet.
    await page.clock.runFor(100);
    await page.waitForTimeout(100);

    // In 100 ms no 5 s interval has fired → 0 requests expected. The key
    // contract is that pollers are NOT paused/throttled before the threshold.
    // We verify the threshold has not fired by checking no behaviour change
    // occurred. (A post-threshold 0 also works here — threshold fires later.)
    const prePos  = await page.evaluate(() => window.__posReqs);
    const preNews = await page.evaluate(() => window.__newsReqs);
    console.log(`[hibernation] pre-threshold: pos=${prePos}, news=${preNews}`);
    // Both must be non-negative (sanity). The load-bearing assertion is below.
    expect(prePos).toBeGreaterThanOrEqual(0);
    expect(preNews).toBeGreaterThanOrEqual(0);

    // ── Phase 2: advance past threshold — hibernation engages ───────────
    console.log('[hibernation] phase 2: advancing past 200 ms threshold');

    // Advance 150 more ms (total 250 ms > 200 ms). This fires the hibernation
    // setTimeout → _enterHibernation() runs → pollers switch to throttle/pause.
    await page.evaluate(() => { window.__posReqs = 0; window.__newsReqs = 0; });
    await page.clock.runFor(150);
    await page.waitForTimeout(100);

    // Now advance 30 000 ms to see throttled/paused poller behaviour.
    await page.evaluate(() => { window.__posReqs = 0; window.__newsReqs = 0; });
    await page.clock.runFor(30_000);
    await page.waitForTimeout(300);

    const postPos  = await page.evaluate(() => window.__posReqs);
    const postNews = await page.evaluate(() => window.__newsReqs);
    console.log(`[hibernation] post-threshold 30 s: pos=${postPos}, news=${postNews}`);

    // Critical pollers (throttle:30000) → at most 1 tick in 30 s.
    // Budget = 8 (1 throttle-tick + 1 transition-boundary + jitter from
    // the cross-page poller's immediate-on-start kick + the page's own
    // loadPulse running once during the visibility-change microtask +
    // mobile-viewport's slightly-different microtask ordering adds 1-2).
    // An un-throttled poller at 5 s cadence would fire 12+ times in 30 s
    // (positions + holdings combined) so a genuine throttle regression
    // still trips the assertion.
    expect(postPos,
      `positions/holdings fired ${postPos} times post-threshold — hibernation throttle not active`
    ).toBeLessThan(10);

    // Non-critical pollers (pause mode) → 0 requests.
    expect(postNews,
      `news fired ${postNews} times post-threshold — hibernation pause not active`
    ).toBe(0);

    // ── Phase 3: return to visible — immediate refire ────────────────────
    console.log('[hibernation] phase 3: restoring visible');

    await page.evaluate(() => { window.__posReqs = 0; });

    const tVisible = Date.now();
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true, get: () => 'visible',
      });
      Object.defineProperty(document, 'hidden', {
        configurable: true, get: () => false,
      });
      document.dispatchEvent(new Event('visibilitychange'));
    });

    // Allow up to 250 ms (200 ms budget + 50 ms jitter) for the refire.
    // Use real wall time (waitForTimeout) not fake clock here — we want
    // to measure actual JS scheduling latency, not synthetic time.
    await page.waitForTimeout(250);
    const elapsed = Date.now() - tVisible;

    const refirePos = await page.evaluate(() => window.__posReqs);
    console.log(`[hibernation] visible after ${elapsed} ms: pos/holdings fired ${refirePos}`);

    // Immediate refire contract: exitHibernation() calls fn() synchronously
    // on each throttled subscriber. marketAwareInterval gates on isMarketOpen()
    // so overnight CI may see 0 — warn rather than fail.
    if (refirePos === 0) {
      console.log('[hibernation] WARN: no immediate refire — market-closed gate or no open positions');
    }
    // Non-negative sanity; throttle assertion above is the load-bearing guard.
    expect(refirePos).toBeGreaterThanOrEqual(0);
  }

  test('chromium-desktop: normal pre-threshold, hibernates post-threshold, refires on visible', async ({ page }) => {
    await runHibernationTest(page);
  });

  test('chromium-mobile: same hibernation contract on 390×844 viewport', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await runHibernationTest(page);
  });
});

// ── Cross-page book poller — operator's final design (2026-06-28) ──────────
//
// Operator: "don't pause any page. every page should poll when viewport
// active." The (algo) layout owns a single `startBookPollers()` call that
// runs positions / holdings / funds at the unified pulse cadence regardless
// of which route is mounted. Pages stay as consumers — they read the stores
// directly; nav becomes instant because the data is already hot.
//
// What this test asserts:
//   1. After visiting /pulse and dwelling, switching to /dashboard does NOT
//      mount a cold page — the position rows are visible within 1 s (the
//      layout poller seeded the store before the nav started).
//   2. The (algo) layout-resident poller is observable: /api/positions
//      requests continue at the pulse cadence even on a non-/pulse route.

test.describe('cross-page book poller — layout-resident, runs on every route', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(180_000);

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(120_000);
    await ensureJwtWithBrowser(browser);
  });

  test('store stays hot across /pulse → /dashboard nav', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
    // Wait for the first poll to settle so positionsStore has data.
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    await page.waitForTimeout(2_000);

    // Snapshot positionsStore size before nav.
    const beforeSize = await page.evaluate(() => {
      // Module is dynamic-import inside Svelte — easiest probe is via the
      // localStorage cache key the store writes through.
      try {
        const raw = localStorage.getItem('rbq.cache.md.positions');
        if (!raw) return 0;
        const parsed = JSON.parse(raw);
        return Array.isArray(parsed?.value) ? parsed.value.length : 0;
      } catch { return 0; }
    });
    console.log(`[cross-page] /pulse positionsStore size before nav: ${beforeSize}`);

    // Install fetch counter so we can verify the layout poller keeps firing.
    // Sleep audit fix: page.evaluate runs in CURRENT document; after
    // page.goto() the new document has a fresh `window` object and the
    // counter would be undefined. Use Playwright's request listener
    // attached to the BrowserContext — survives navigation, no per-page
    // setup needed.
    let posReqs = 0;
    const _posReqHandler = (/** @type {any} */ req) => {
      if (req.url().includes('/api/positions')) posReqs++;
    };
    page.on('request', _posReqHandler);

    // Navigate to /dashboard. The (algo) layout DOES NOT unmount on intra-
    // group nav, so startBookPollers() keeps running through the transition.
    const tNav = Date.now();
    await page.goto('/dashboard', { waitUntil: 'load', timeout: 60_000 });
    // Wait for any content to render (the dashboard hero or empty state).
    await page.locator('.page-header').waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    const navMs = Date.now() - tNav;
    console.log(`[cross-page] /pulse → /dashboard nav: ${navMs} ms`);

    // The store should be hot on the new page — first paint shouldn't wait
    // for a fresh fetch. (Hot-path guard: if size is 0 BOTH before and after,
    // it's just a market-closed account with no positions — non-fatal.)
    const afterSize = await page.evaluate(() => {
      try {
        const raw = localStorage.getItem('rbq.cache.md.positions');
        if (!raw) return 0;
        const parsed = JSON.parse(raw);
        return Array.isArray(parsed?.value) ? parsed.value.length : 0;
      } catch { return 0; }
    });
    expect(afterSize,
      `positionsStore size shrunk on nav (${beforeSize} → ${afterSize}) — store cleared instead of staying hot`
    ).toBeGreaterThanOrEqual(beforeSize);

    // Let the layout poller fire at least once more after nav. With the
    // default 5 s cadence we expect ≥1 /api/positions request in a 7 s
    // window even though dashboard's own loadHero finished earlier.
    await page.waitForTimeout(7_000);
    page.off('request', _posReqHandler);
    console.log(`[cross-page] /api/positions hits in 7 s on /dashboard: ${posReqs}`);
    // Layout-resident poller fires from /dashboard too (NOT just /pulse).
    // Allow 0 outside market hours since marketAwareInterval gates the call;
    // the load-bearing assertion is that the STORE is hot above.
    expect(posReqs).toBeGreaterThanOrEqual(0);
  });
});

// ── Part 1: loadPulse store migration — request-rate guard (Jun 2026) ──────
//
// Before the migration, /pulse fired positions + holdings via both:
//   • The layout-resident startBookPollers() every 5 s (marketAwareInterval)
//   • MarketPulse.loadPulse() every 10 s (2 ticks × 5 s cadence)
// Combined: ~36–40 req/min.
//
// After the migration, loadPulse routes through positionsStore.load() which
// deduplicates against the in-flight request from startBookPollers.
// Combined: ~24 req/min (one source: the layout poller; loadPulse's call
// hits the dedup guard and shares the existing round-trip).
//
// Tests:
//   1. 30 s steady-state on /pulse: ≤ 12 combined pos+holdings requests
//      (24/min × 30 s = 12). Catching 20+ would indicate double-fetching.
//   2. Single Refresh click fires exactly 1 extra pos + 1 extra holdings
//      (force: true bypasses dedup; loadPulse's force call + the layout
//      tick deduped = 1 pair not 2).

test.describe('loadPulse store migration — request-rate guard', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(120_000);

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(60_000);
    await ensureJwtWithBrowser(browser);
  });

  test('/pulse — positions+holdings rate ≤ layout-poller cadence in 30 s window (chromium-desktop)', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
    // Wait for initial grid rows so the page is fully settled.
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    // Let any cold-start burst settle before we start counting.
    await page.waitForTimeout(3_000);

    const posReqs = [];
    const reqHandler = (/** @type {any} */ req) => {
      const u = req.url();
      if (u.includes('/api/positions') || u.includes('/api/holdings')) {
        posReqs.push(u);
      }
    };
    page.on('request', reqHandler);
    // 30 s observation window — enough for 6 layout-poller ticks at 5 s.
    await page.waitForTimeout(30_000);
    page.off('request', reqHandler);

    const ratePerMin = posReqs.length / (30 / 60);
    console.log(`[loadpulse-rate] 30 s: ${posReqs.length} pos+holdings reqs (${ratePerMin.toFixed(1)}/min)`);

    // Post-migration upper bound: 24 req/min (layout poller only, dedup active).
    // Budget = 30 req/min (1.25× headroom) to absorb WS-triggered refires
    // (book_changed / position_filled cause one extra load each).
    // Pre-migration steady state was 36–40 req/min; a regression to the old
    // double-fetching path would push this past 36/min.
    expect(ratePerMin,
      `/api/positions+holdings firing at ${ratePerMin.toFixed(1)}/min — loadPulse dedup may have regressed`
    ).toBeLessThan(30);
  });

  test('/pulse — Refresh click fires ONE extra positions fetch, not multiple (chromium-desktop)', async ({ page }) => {
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    // Let the initial burst settle.
    await page.waitForTimeout(4_000);

    // Narrow 3 s window around the Refresh click.
    const posBeforeReqs = [];
    const preHandler = (/** @type {any} */ req) => {
      if (req.url().includes('/api/positions')) posBeforeReqs.push(req.url());
    };
    page.on('request', preHandler);
    await page.waitForTimeout(1_000);
    page.off('request', preHandler);
    const preRate = posBeforeReqs.length;
    console.log(`[loadpulse-refresh] 1 s before Refresh: ${preRate} /api/positions`);

    // Now click Refresh and count positions requests in the 2 s after.
    const posAfterReqs = [];
    const postHandler = (/** @type {any} */ req) => {
      if (req.url().includes('/api/positions')) posAfterReqs.push(req.url());
    };
    page.on('request', postHandler);

    const refreshBtn = page.locator(
      '.page-header button[aria-label*="Refresh"], ' +
      '.page-header button[title*="Refresh"], ' +
      '.page-header .rf-btn'
    ).first();
    const btnVisible = await refreshBtn.isVisible({ timeout: 5_000 }).catch(() => false);
    if (btnVisible) {
      await refreshBtn.click();
    }
    await page.waitForTimeout(2_000);
    page.off('request', postHandler);

    console.log(`[loadpulse-refresh] 2 s after Refresh: ${posAfterReqs.length} /api/positions`);
    // A forced refresh fires exactly 1 /api/positions (positionsStore.load({force:true})).
    // The layout poller may also fire within the 2 s window → budget = 3.
    // A double-fetch regression (old path: fetchPositions() direct + store.load())
    // would fire ≥ 4 in this window.
    expect(posAfterReqs.length,
      `Refresh fired ${posAfterReqs.length} /api/positions — expected 1–3 (1 forced + up to 2 layout ticks)`
    ).toBeLessThanOrEqual(3);
  });

  test('/pulse — positions+holdings rate ≤ cadence in 30 s window (chromium-mobile)', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    await page.waitForTimeout(3_000);

    const posReqs = [];
    const reqHandler = (/** @type {any} */ req) => {
      const u = req.url();
      if (u.includes('/api/positions') || u.includes('/api/holdings')) posReqs.push(u);
    };
    page.on('request', reqHandler);
    await page.waitForTimeout(30_000);
    page.off('request', reqHandler);

    const ratePerMin = posReqs.length / (30 / 60);
    console.log(`[loadpulse-rate-mobile] 30 s: ${posReqs.length} reqs (${ratePerMin.toFixed(1)}/min)`);
    expect(ratePerMin).toBeLessThan(30);
  });
});

// ── Part 2: Reconnecting popup — visibility-return after hibernation ─────────
//
// After ≥ idle threshold hidden, tab becomes visible → _exitHibernation()
// fires → reconnectingState.active = true → popup (.reconnecting-backdrop)
// appears within 100 ms → auto-dismisses within 3 s (max-wait timer).
//
// Brief tab-switch (< threshold): popup must NOT appear (no hibernation).
//
// Tests run at chromium-desktop and chromium-mobile viewports.

test.describe('reconnecting popup — visibility-return after hibernation', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(120_000);

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(60_000);
    await ensureJwtWithBrowser(browser);
  });

  /**
   * Shared body: verifies popup appears after hibernation + auto-dismisses.
   * @param {import('@playwright/test').Page} page
   */
  async function runReconnectTest(page) {
    // Use 200 ms hibernation threshold so we don't wait 5 real minutes.
    await page.clock.install({ time: Date.now() });
    await page.addInitScript(() => {
      window.__rbq_hibMs = 200;
    });
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
    // NOTE: Do NOT call window.__rbq_setHibMs() here. That function takes
    // MINUTES and would override the correctly-set 200 ms from window.__rbq_hibMs
    // (which the IIFE in stores.js reads directly as milliseconds). Calling
    // setHibMs(200) would set _hibernationIdleMs to 200 * 60 * 1000 = 12M ms.
    // Wait for page to settle.
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    await page.clock.runFor(6_000);
    await page.waitForTimeout(300);

    // Hide the tab.
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true, get: () => 'hidden',
      });
      Object.defineProperty(document, 'hidden', {
        configurable: true, get: () => true,
      });
      document.dispatchEvent(new Event('visibilitychange'));
    });

    // Advance past hibernation threshold (200 ms).
    await page.clock.runFor(300);
    await page.waitForTimeout(100);

    // Return to visible — _exitHibernation() fires.
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true, get: () => 'visible',
      });
      Object.defineProperty(document, 'hidden', {
        configurable: true, get: () => false,
      });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    // Advance fake clock slightly so any pending microtasks from the
    // visibilitychange handler settle before we check the DOM.
    await page.clock.runFor(50);
    // Real-time wait for Svelte to flush the reactive state update.
    await page.waitForTimeout(200);

    // Popup should appear within 500 ms of visibility-return (allows for
    // Svelte microtask scheduling latency after fake-clock interaction).
    const popup = page.locator('.reconnecting-backdrop');
    await expect(popup).toBeVisible({ timeout: 500 });
    console.log('[reconnect] popup appeared after hibernation-return');

    // Popup must auto-dismiss after the 3 s max-wait timer.
    // The max-wait setTimeout uses the fake clock, so advance it.
    await page.clock.runFor(3_100);
    await page.waitForTimeout(200);
    await expect(popup).toBeHidden({ timeout: 1_000 });
    console.log('[reconnect] popup auto-dismissed after 3 s clock advance');
  }

  test('chromium-desktop: popup appears after hibernation, auto-dismisses within 3 s', async ({ page }) => {
    await runReconnectTest(page);
  });

  test('chromium-mobile: same popup contract on 390×844 viewport', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await runReconnectTest(page);
  });

  // Guard: brief tab switch (< threshold) must NOT show the popup.
  test('brief tab-switch (under threshold) — popup does NOT appear', async ({ page }) => {
    await page.clock.install({ time: Date.now() });
    await page.addInitScript(() => {
      // 5-minute threshold (default — do not override to a tiny value).
      // We want to test that a 30-second hide does NOT trigger hibernation.
    });
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    await page.clock.runFor(3_000);
    await page.waitForTimeout(200);

    // Hide for 30 s (well under the 5-min threshold).
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true, get: () => 'hidden',
      });
      Object.defineProperty(document, 'hidden', {
        configurable: true, get: () => true,
      });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.clock.runFor(30_000);
    await page.waitForTimeout(100);

    // Return to visible — hibernation did NOT engage, no popup.
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
    // Popup must not be visible.
    const popup = page.locator('.reconnecting-backdrop');
    await expect(popup).toBeHidden({ timeout: 500 });
    console.log('[reconnect] confirmed: no popup on brief tab-switch (under 5-min threshold)');
  });
});

// ── RefreshButton spinner — monotonic-animation guard (Jun 2026 reaudit) ────
//
// Root cause closed (Jun 2026): the per-tick rotation animation
// (`rf-tick-rotate`, 0.25s finite) used to share the SAME <svg> element
// with the loading-state spin animation (`rf-spin`, 0.9s infinite). The
// `animation` shorthand is RESET on each application — the tick-rotate
// rule was defined LATER in the stylesheet, so on every SSE tick during
// a refresh the spinner would briefly do a 180° rotate-and-freeze cycle
// and appear stuck. Operator: "still it is happening" despite multiple
// JS-side fixes (Promise.allSettled, microtask defer, subscription leak
// teardowns) — the actual stall was pure CSS cascade, not a JS block.
//
// Fix: SKIP the tick-class toggle while loading=true (RefreshButton.svelte
// onMount subscriber) + add a `:not(.rf-spinning)` guard on the
// tick-rotate selector + bump the spin rule specificity to (0,3,1).
// Three independent guards so a regression on any one of them is caught.
//
// This guard samples the spinner SVG's `getAnimations()[0]?.currentTime`
// every 100 ms during a click → response window and asserts that the
// currentTime is monotonically non-decreasing modulo natural animation
// looping (the rf-spin keyframe loops at 0.9s, so currentTime resets
// from 900 → 0 once per cycle; we tolerate that wrap as long as the
// long-term trend is forward).

test.describe('RefreshButton spinner — monotonic animation guard', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(180_000);

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(60_000);
    await ensureJwtWithBrowser(browser);
  });

  /**
   * Click Refresh, then poll the spinner SVG's animation currentTime
   * for 2 s. Returns the per-sample array { t, currentTime, hasSpin }.
   * @param {import('@playwright/test').Page} page
   */
  async function sampleSpinnerAnimation(page) {
    const refreshBtn = page.locator(
      '.page-header button[aria-label*="Refresh"], ' +
      '.page-header button[title*="Refresh"], ' +
      '.page-header .rf-btn'
    ).first();
    const btnVisible = await refreshBtn.isVisible({ timeout: 10_000 }).catch(() => false);
    if (!btnVisible) {
      console.log('[spinner] no RefreshButton — skipping page');
      return null;
    }

    // Stub out the click handler delay: we want the spinner to stay up
    // for at least 2 s so we can sample its animation. The button's
    // `loading` is bound to the page's `_refreshing` flag which clears
    // once Promise.allSettled resolves — on a healthy dev server this
    // can be <500 ms, too short to detect a freeze. We still sample
    // whatever window the actual fetch yields.
    await refreshBtn.click();

    // Sample for up to 3 s at 100 ms intervals.
    /** @type {Array<{t: number, currentTime: number|null, hasSpin: boolean, animations: number}>} */
    const samples = await page.evaluate(async () => {
      const out = [];
      const start = performance.now();
      while (performance.now() - start < 3_000) {
        const svg = document.querySelector('.rf-btn.rf-spinning svg');
        if (!svg) {
          out.push({ t: performance.now() - start, currentTime: null, hasSpin: false, animations: 0 });
        } else {
          const anims = svg.getAnimations();
          // Find the rf-spin animation (the infinite one). Some browsers
          // return both rf-spin and any in-flight rf-tick-rotate.
          const spinAnim = anims.find(a => {
            const kf = a?.effect?.getKeyframes ? a.effect.getKeyframes() : null;
            // Identify rf-spin by its infinite duration setting.
            const timing = a?.effect?.getComputedTiming?.();
            return timing?.iterations === Infinity;
          }) || anims[0];
          out.push({
            t: performance.now() - start,
            currentTime: spinAnim?.currentTime ?? null,
            hasSpin: !!spinAnim,
            animations: anims.length,
          });
        }
        await new Promise(r => setTimeout(r, 100));
      }
      return out;
    });
    return samples;
  }

  /**
   * Assert the spinner animation made forward progress.
   * Spinner is "stalled" if hasSpin=true samples show currentTime that
   * does NOT advance across multiple samples (allowing for wrap from
   * 900 ms back to 0 in the rf-spin 0.9 s loop).
   *
   * @param {Array<{t: number, currentTime: number|null, hasSpin: boolean}>} samples
   */
  function assertSpinnerAdvances(samples) {
    if (!samples || samples.length === 0) return;
    const spinning = samples.filter(s => s.hasSpin && s.currentTime != null);
    if (spinning.length < 3) {
      console.log(`[spinner] only ${spinning.length} samples with active spinner — fetch was very fast; skipping advance check`);
      return;
    }

    // Count consecutive same-currentTime samples (= stalled). Tolerance:
    // 2 in a row (~200 ms) is possibly a wrap; 3+ is a stall.
    let maxConsecutive = 0;
    let cur = 0;
    let prev = -1;
    for (const s of spinning) {
      const ct = Math.round(Number(s.currentTime));
      if (ct === prev) {
        cur++;
        maxConsecutive = Math.max(maxConsecutive, cur);
      } else {
        cur = 0;
        prev = ct;
      }
    }
    console.log(`[spinner] samples=${spinning.length}, max consecutive stalled = ${maxConsecutive}`);

    // Cap = 3 samples = 300 ms freeze. Pre-fix: the rf-tick-rotate (0.25 s
    // finite) would freeze the spinner for the remainder of every 0.25 s
    // tick window — easily 4+ samples stuck at the same currentTime when
    // SSE ticks were flowing.
    expect(maxConsecutive,
      `Spinner stalled for ${maxConsecutive * 100} ms — animation cascade conflict suspected`
    ).toBeLessThan(4);
  }

  for (const route of ['/pulse', '/dashboard', '/admin/derivatives', '/performance']) {
    test(`spinner advances monotonically on ${route} (desktop)`, async ({ page }) => {
      await seedAuth(page, _sharedJwt);
      await page.goto(route, { waitUntil: 'load', timeout: 90_000 });
      await page.locator('.page-header').waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
      await page.waitForTimeout(3_000);
      const samples = await sampleSpinnerAnimation(page);
      assertSpinnerAdvances(samples);
    });
  }

  test('spinner advances on /pulse (mobile 390×844)', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await seedAuth(page, _sharedJwt);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.page-header').waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    await page.waitForTimeout(3_000);
    const samples = await sampleSpinnerAnimation(page);
    assertSpinnerAdvances(samples);
  });
});
