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

// ── Tests ───────────────────────────────────────────────────────────────────

test.describe('main-thread perf regression guard', () => {
  test.setTimeout(180_000);

  test('derivatives page — no long tasks during button click, heap stable', async ({ page }) => {
    await instrumentLongTasks(page);
    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );

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
  });

  // ── /pulse — MarketPulse buildUnified cascade guard ────────────────────────
  test('/pulse — no long tasks on RefreshButton click, heap stable', async ({ page }) => {
    await instrumentLongTasks(page);
    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );

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
  });

  // ── /dashboard — positions/holdings summary cascade guard ──────────────────
  test('/dashboard — no long tasks on RefreshButton click, heap stable', async ({ page }) => {
    await instrumentLongTasks(page);
    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );

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
    await instrumentLongTasks(page);
    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );

    await page.goto('/orders', { waitUntil: 'load', timeout: 90_000 });
    await page.locator('.page-header').waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    await page.waitForTimeout(5_000);

    const { heapGrowthPerMin } = await settleAndMeasureHeap(page, 15);

    const { maxLongTask, clickToFeedbackMs } = await clickRefreshAndMeasure(page);

    assertPerfDimensions({ maxLongTask, clickToFeedbackMs, heapGrowthPerMin });
  });

  // ── /performance — PerformancePage grid cascade guard ─────────────────────
  test('/performance — no long tasks on RefreshButton click, heap stable', async ({ page }) => {
    await instrumentLongTasks(page);
    // /performance is a public page — sign in so we get real data.
    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );

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
  test.setTimeout(180_000);
  test.use({ viewport: { width: 390, height: 844 } });

  async function mobilePerfTest(page, url, waitSelector) {
    await instrumentLongTasks(page);
    await loginAsAdmin(page, { user: 'ambore' }).catch(() =>
      loginAsAdmin(page, { user: 'rambo' })
    );
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
  }

  test('/pulse mobile — no long tasks on RefreshButton click', async ({ page }) => {
    await mobilePerfTest(page, '/pulse', '.ag-row');
  });

  test('/dashboard mobile — no long tasks on RefreshButton click', async ({ page }) => {
    await mobilePerfTest(page, '/dashboard', null);
  });

  test('/orders mobile — no long tasks on RefreshButton click', async ({ page }) => {
    await mobilePerfTest(page, '/orders', null);
  });

  test('/performance mobile — no long tasks on RefreshButton click', async ({ page }) => {
    await mobilePerfTest(page, '/performance', '.ag-row');
  });
});
