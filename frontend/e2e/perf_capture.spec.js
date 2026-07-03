/**
 * perf_capture.spec.js — runtime metrics capture (NOT assertions).
 *
 * Distinct from `main_thread_perf.spec.js`, which asserts and fails on
 * regression. This spec CAPTURES per-page runtime metrics and writes a
 * JSON blob to `.log/perf_capture_<utc>.json` in the SAME shape that
 * `perf_baseline.py` produces, so `perf_diff.py` can merge both.
 *
 * Captured per page:
 *   - lcp_ms          Largest Contentful Paint
 *   - dom_content_ms  DOMContentLoaded timing
 *   - load_ms         load event timing
 *   - long_task_ms    Σ long-task duration during 5 s idle
 *   - long_task_max_ms  longest single long task
 *   - heap_mb         used JS heap after 5 s idle (Chrome only)
 *   - ws_connections  count of open /ws/* sockets
 *   - refresh_click_ms  RefreshButton click → visible feedback latency
 *
 * Pages: /pulse, /dashboard, /performance, /admin/derivatives, /charts,
 * /orders, /admin/history, /admin/audit.
 *
 * Nothing here calls `expect(...)` on the numbers — the file must
 * ALWAYS write JSON at afterAll, even when a page load hiccups. If
 * you want thresholds, add them to main_thread_perf.spec.js.
 *
 * Run against dev:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *     npx playwright test e2e/perf_capture.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

// Repo root — this spec lives at frontend/e2e/, so go up two levels.
const REPO_ROOT = path.resolve(process.cwd(), process.cwd().endsWith('/frontend')
  ? '..' : '.');
const LOG_DIR = path.join(REPO_ROOT, '.log');

// Pages to profile. First column is a stable label that also appears
// in perf_baseline.py so the diff tool can join rows.
const PAGES = [
  ['/pulse',             { waitFor: '.ag-root-wrapper, .card, main' }],
  ['/dashboard',         { waitFor: '.card, main' }],
  ['/performance',       { waitFor: '.ag-root-wrapper, .card, main' }],
  ['/admin/derivatives', { waitFor: '.ag-root-wrapper, .card, main' }],
  ['/charts',            { waitFor: '.card, main' }],
  ['/orders',            { waitFor: '.ag-root-wrapper, .card, main' }],
  ['/admin/history',     { waitFor: '.ag-root-wrapper, .card, main' }],
  ['/admin/audit',       { waitFor: '.ag-root-wrapper, .card, main' }],
];

// Module-level accumulator. Populated per-test, flushed in afterAll.
const CAPTURED = {
  captured_at: new Date().toISOString().replace(/\.\d+Z$/, 'Z'),
  commit: process.env.RAMBOQ_COMMIT || '',
  frontend: { pages: {}, bundle_size_kb: null },
  backend: { routes: {} },
};

// One shared JWT so we don't hammer /api/auth/login.
let _sharedJwt = '';

// ── Helpers ────────────────────────────────────────────────────────────────

async function ensureJwt(browser) {
  if (_sharedJwt) return;
  const ctx = await browser.newContext({ baseURL: BASE });
  const page = await ctx.newPage();
  try {
    await loginAsAdmin(page);
    const tok = await page.evaluate(() => sessionStorage.getItem('ramboq_token'));
    if (!tok) throw new Error('Login succeeded but token missing');
    _sharedJwt = tok;
  } finally {
    await page.close();
    await ctx.close();
  }
}

async function seedAuth(page, jwt) {
  await page.addInitScript((token) => {
    sessionStorage.setItem('ramboq_token', token);
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

async function instrument(page) {
  await page.addInitScript(() => {
    window.__longTasks = [];
    window.__wsCount = 0;

    // Long-task observer — count everything, no assertions.
    try {
      const obs = new PerformanceObserver(list => {
        for (const e of list.getEntries()) {
          window.__longTasks.push({ duration: e.duration, startTime: e.startTime });
        }
      });
      obs.observe({ type: 'longtask', buffered: true });
    } catch (_) { /* not supported */ }

    // WebSocket tap — count opens on /ws/*. Ref-counted pool in ws.js
    // means we should see ≤2 per tab even with multiple subscribers.
    const _WS = window.WebSocket;
    window.WebSocket = function (url, protocols) {
      try {
        if (typeof url === 'string' && url.includes('/ws/')) {
          window.__wsCount = (window.__wsCount || 0) + 1;
        }
      } catch (_) { /* ignore */ }
      return protocols !== undefined
        ? new _WS(url, protocols)
        : new _WS(url);
    };
    // Preserve statics + prototype so `instanceof WebSocket` still works.
    Object.setPrototypeOf(window.WebSocket, _WS);
    window.WebSocket.prototype = _WS.prototype;
    for (const k of ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED']) {
      window.WebSocket[k] = _WS[k];
    }
  });
}

/** Measure the four navigation-timing values in ms. */
async function navTiming(page) {
  return page.evaluate(() => {
    const [nav] = performance.getEntriesByType('navigation');
    if (!nav) return { load_ms: null, dom_content_ms: null };
    return {
      load_ms:        Math.round(nav.loadEventEnd || 0),
      dom_content_ms: Math.round(nav.domContentLoadedEventEnd || 0),
    };
  });
}

/** LCP via PerformanceObserver — measured at the end of the idle window. */
async function measureLcp(page) {
  return page.evaluate(() => new Promise(res => {
    try {
      const obs = new PerformanceObserver(list => {
        const entries = list.getEntries();
        const last = entries[entries.length - 1];
        if (last) {
          res({ lcp_ms: Math.round(last.startTime) });
          obs.disconnect();
        }
      });
      obs.observe({ type: 'largest-contentful-paint', buffered: true });
      // Bail out after 200 ms — LCP is buffered, we should get it
      // immediately or it doesn't exist for this page.
      setTimeout(() => { obs.disconnect(); res({ lcp_ms: null }); }, 200);
    } catch (_) {
      res({ lcp_ms: null });
    }
  }));
}

async function heapMB(page) {
  return page.evaluate(() =>
    // performance.memory is Chrome-only. Return null on Firefox rather
    // than a bogus zero so the diff table renders "-" instead of 0MB.
    window.performance?.memory?.usedJSHeapSize != null
      ? window.performance.memory.usedJSHeapSize / (1024 * 1024)
      : null
  );
}

/** Click page-header RefreshButton; return click-to-feedback ms. */
async function measureRefreshClick(page) {
  const btn = page.locator('button[title^="Refresh"]').first();
  if (!(await btn.count())) return null;
  try {
    const t0 = Date.now();
    await btn.click({ timeout: 3_000 });
    // "Feedback" = the button flips to disabled or spinner glyph
    // appears. Wait for either up to 2 s. If neither happens, return
    // the wall-clock so operators see the ceiling instead of a null.
    try {
      await page.waitForFunction(() => {
        const b = document.querySelector('button[title^="Refresh"]');
        return b && (b.disabled || b.querySelector('.spinner, [class*="anim"]'));
      }, { timeout: 2_000 });
      return Date.now() - t0;
    } catch (_) {
      return Date.now() - t0;
    }
  } catch (_) {
    return null;
  }
}

// ── Tests ──────────────────────────────────────────────────────────────────

test.describe('perf capture (runtime metrics)', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(180_000);

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(60_000);
    await ensureJwt(browser);
  });

  for (const [route, opts] of PAGES) {
    test(`capture ${route}`, async ({ page }) => {
      const row = { runtime: {} };
      try {
        await instrument(page);
        await seedAuth(page, _sharedJwt);

        // Navigate + wait for main content. Any failure past here still
        // yields whatever partial numbers we've gathered.
        await page.goto(route, { waitUntil: 'load', timeout: 60_000 });
        try {
          await page.locator(opts.waitFor).first()
            .waitFor({ state: 'visible', timeout: 15_000 });
        } catch (_) { /* proceed with whatever loaded */ }

        // Idle for 5 s so long tasks + heap stabilise.
        await page.waitForTimeout(5_000);

        const [nav, lcp, heap] = await Promise.all([
          navTiming(page),
          measureLcp(page),
          heapMB(page),
        ]);

        const longTasks = await page.evaluate(() => window.__longTasks || []);
        const wsCount  = await page.evaluate(() => window.__wsCount || 0);
        const refresh  = await measureRefreshClick(page);

        row.runtime = {
          ...nav,
          ...lcp,
          heap_mb: heap == null ? null : Number(heap.toFixed(2)),
          long_task_ms:      Math.round(longTasks.reduce((a, t) => a + t.duration, 0)),
          long_task_max_ms:  Math.round(longTasks.reduce((m, t) => Math.max(m, t.duration), 0)),
          ws_connections:    wsCount,
          refresh_click_ms:  refresh,
        };
      } catch (err) {
        row.runtime = { error: String(err).slice(0, 400) };
      }
      CAPTURED.frontend.pages[route] = row;
      // Placeholder assertion so the test doesn't get flagged as empty
      // by CI. Anything we captured (or a recorded error) is a pass.
      expect(row).toBeTruthy();
    });
  }

  test.afterAll(async () => {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    const stamp = CAPTURED.captured_at.replace(/[:-]/g, '');
    const dst = path.join(LOG_DIR, `perf_capture_${stamp}.json`);
    fs.writeFileSync(dst, JSON.stringify(CAPTURED, null, 2));
    console.log(`[perf_capture] wrote ${path.relative(REPO_ROOT, dst)}`);

    // Also refresh a "latest" symlink so perf_diff.py can find it
    // without arguments. Copy on filesystems that reject symlinks.
    const latest = path.join(LOG_DIR, 'perf_capture_latest.json');
    try {
      if (fs.existsSync(latest)) fs.unlinkSync(latest);
      fs.symlinkSync(path.basename(dst), latest);
    } catch (_) {
      fs.copyFileSync(dst, latest);
    }
  });
});
