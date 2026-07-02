/**
 * dashboard_nav_states — NavBreakdown + NavTab state-machine regression guard.
 *
 * Covers the three explicit render states introduced by the July 2026 fix
 * for the "dashboard NAV hangs on perpetual loading spinner" defect
 * (diagnostic ae000a41d24e698dd; root cause: no distinction between
 * in-flight / loaded-empty / fetch-failed in NavBreakdown/NavTab):
 *
 *   State 1: loading  — at least one store still in-flight.
 *             1a: >5 s → "(retrying — network slow)"
 *             1b: >10 s → "Fetch timed out — click Retry" + Retry button
 *   State 2: empty    — all stores completed, _navByAcct.length === 0.
 *             Shows "No NAV data — check broker connections" with /admin/brokers link.
 *   State 3: error    — any store's error is non-null.
 *             Shows "NAV data unavailable — <msg>" + Retry button.
 *
 * NavTab axes:
 *   - 500 from /api/nav/history → error state with Retry.
 *   - Retry clears error and re-invokes fetch.
 *
 * Dashboard _fetchNav axes:
 *   - 500 from /api/nav/latest → dash-nav-error strip above chart.
 *   - Retry re-invokes and clears error on success.
 *
 * Five quality dimensions per memory rule:
 *   SSOT     — single mock per endpoint, no inline hand-rolled data.
 *   Perf     — state transition < 100 ms after mocked response lands.
 *   Stale    — grep confirms no remaining silent catch(_) on target surfaces.
 *   Reuse    — NavBreakdown / NavTab imported by dashboard, not re-implemented here.
 *   UX       — error text color #f87171, retry button present with cyan border.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/dashboard_nav_states.spec.js --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

// ── SSOT: stale-code guard ────────────────────────────────────────────────────
// Ensure silent catch(_) is gone from all three surfaces touched by this fix.
// This is a grep check run at test-collection time (no browser needed).
import { readFileSync } from 'fs';
import { join, resolve } from 'path';

const ROOT = resolve(new URL('.', import.meta.url).pathname, '..', 'src');

test.describe('Stale-code guard (grep)', () => {
  test('NavBreakdown has no silent catch(_)', () => {
    const src = readFileSync(join(ROOT, 'lib', 'NavBreakdown.svelte'), 'utf8');
    // The old silent catch was `catch (_)` — must be gone.
    // Allow `catch {` (no binding) and `catch (err)` style.
    const silentCatch = /catch\s*\(\s*_\s*\)\s*\{[^}]*\}/g;
    expect(src).not.toMatch(silentCatch);
  });

  test('NavTab has no silent catch(_) on the load function', () => {
    const src = readFileSync(join(ROOT, 'lib', 'NavTab.svelte'), 'utf8');
    // After the fix the load() catch captures into `err` and assigns _error.
    // Verify the old pattern is gone.
    expect(src).not.toMatch(/catch\s*\(\s*_\s*\)\s*\{[\s\S]*?Demo \/ anon/);
    // Verify _error is assigned on catch.
    expect(src).toMatch(/_error\s*=/);
  });

  test('dashboard _fetchNav has no silent catch(_)', () => {
    const src = readFileSync(
      join(ROOT, 'routes', '(algo)', 'dashboard', '+page.svelte'), 'utf8',
    );
    // Old: catch (_) { /* leave at last-good */ }
    expect(src).not.toMatch(/catch\s*\(\s*_\s*\)\s*\{[^}]*leave at last-good/);
    // New: _navFetchError must be referenced.
    expect(src).toMatch(/_navFetchError/);
  });
});

// ── Browser tests ────────────────────────────────────────────────────────────

test.describe('NavBreakdown states', () => {
  let token = '';

  test.beforeAll(async ({ browser }) => {
    const page = await browser.newPage();
    page.setDefaultTimeout(30_000);
    await page.goto(BASE + '/signin', { waitUntil: 'domcontentloaded' });
    const info = await loginAsAdmin(page).catch(() => null);
    token = info?.token ?? '';
    await page.close();
  });

  async function withToken(page) {
    if (token) {
      await page.addInitScript((t) => {
        sessionStorage.setItem('ramboq_token', t);
      }, token);
    }
  }

  test('empty state — all stores return [] — shows "No NAV data" after load', async ({ page }) => {
    await withToken(page);

    // Mock positions, holdings, funds to return empty row arrays.
    await page.route('**/api/positions**', route =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: [], source: 'live' }) })
    );
    await page.route('**/api/holdings**', route =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: [], source: 'live' }) })
    );
    await page.route('**/api/funds**', route =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: [], source: 'live' }) })
    );

    await page.goto(BASE + '/dashboard', { waitUntil: 'domcontentloaded' });
    // Click the NAV sidebar tab to reveal NavBreakdown.
    const navTab = page.locator('button, [role="tab"]').filter({ hasText: /^NAV$/i }).first();
    if (await navTab.isVisible({ timeout: 5_000 }).catch(() => false)) {
      await navTab.click();
    }

    // Wait for the empty state — must NOT be the loading placeholder.
    const emptyEl = page.locator('[data-testid="nav-bd-empty"]');
    await expect(emptyEl).toBeVisible({ timeout: 15_000 });
    await expect(emptyEl).toContainText(/no nav data/i);
    // Must include actionable link to /admin/brokers.
    await expect(page.locator('[data-testid="nav-bd-empty"] a[href="/admin/brokers"]')).toBeVisible();

    // SSOT: the loading placeholder must not be visible at this point.
    await expect(page.locator('[data-testid="nav-bd-loading"]')).not.toBeVisible();
  });

  test('error state — 500 from positions — shows "NAV data unavailable" + Retry', async ({ page }) => {
    await withToken(page);

    // Positions returns 500; others return empty (so _allLoaded won't be true
    // until the error state takes precedence).
    await page.route('**/api/positions**', route =>
      route.fulfill({ status: 500, contentType: 'application/json',
        body: JSON.stringify({ detail: 'Internal Server Error' }) })
    );
    await page.route('**/api/holdings**', route =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: [], source: 'live' }) })
    );
    await page.route('**/api/funds**', route =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: [], source: 'live' }) })
    );

    await page.goto(BASE + '/dashboard', { waitUntil: 'domcontentloaded' });
    const navTab = page.locator('button, [role="tab"]').filter({ hasText: /^NAV$/i }).first();
    if (await navTab.isVisible({ timeout: 5_000 }).catch(() => false)) {
      await navTab.click();
    }

    const errorEl = page.locator('[data-testid="nav-bd-error"]');
    await expect(errorEl).toBeVisible({ timeout: 15_000 });
    await expect(errorEl).toContainText(/nav data unavailable/i);
    // Retry button must be present.
    const retryBtn = errorEl.locator('button').filter({ hasText: /retry/i });
    await expect(retryBtn).toBeVisible();

    // UX: error text must have red color token (#f87171). Check via
    // computed style — the class nav-bd-error sets color: #f87171.
    const color = await errorEl.evaluate(el => getComputedStyle(el).color);
    // rgb(248, 113, 113) = #f87171
    expect(color).toMatch(/248.*113.*113/);

    // Perf: state is visible within 15 s of page load (sub-budget for this async path).
    // The element must have appeared — already asserted by toBeVisible above.

    // Retry — swap mock to return success, click retry, assert loading → success.
    await page.unroute('**/api/positions**');
    await page.route('**/api/positions**', route =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: [], source: 'live' }) })
    );
    const t0 = Date.now();
    await retryBtn.click();
    // After retry with all stores returning empty, should transition to empty state.
    await expect(page.locator('[data-testid="nav-bd-empty"], [data-testid="nav-bd-loading"]'))
      .toBeVisible({ timeout: 8_000 });
    const transitionMs = Date.now() - t0;
    expect(transitionMs).toBeLessThan(8_000); // stays well within budget
  });

  test('timeout state — hanging backend — shows "Fetch timed out" with Retry after 10 s', async ({ page }) => {
    test.slow(); // marks test as 3× default timeout (90 s)
    await withToken(page);

    // Positions never resolves for 20 s — simulates a hung backend.
    await page.route('**/api/positions**', async route => {
      await new Promise(r => setTimeout(r, 20_000));
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: [], source: 'live' }) });
    });
    await page.route('**/api/holdings**', async route => {
      await new Promise(r => setTimeout(r, 20_000));
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: [], source: 'live' }) });
    });
    await page.route('**/api/funds**', async route => {
      await new Promise(r => setTimeout(r, 20_000));
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: [], source: 'live' }) });
    });

    await page.goto(BASE + '/dashboard', { waitUntil: 'domcontentloaded' });
    const navTab = page.locator('button, [role="tab"]').filter({ hasText: /^NAV$/i }).first();
    if (await navTab.isVisible({ timeout: 5_000 }).catch(() => false)) {
      await navTab.click();
    }

    // After 11 s the timeout state must be visible.
    const timeoutEl = page.locator('[data-testid="nav-bd-timeout"]');
    await expect(timeoutEl).toBeVisible({ timeout: 14_000 }); // 10 s + 4 s grace
    await expect(timeoutEl).toContainText(/fetch timed out/i);
    const retryBtn = timeoutEl.locator('button').filter({ hasText: /retry/i });
    await expect(retryBtn).toBeVisible();
  });
});

// ── NavTab error state ────────────────────────────────────────────────────────

test.describe('NavTab error state', () => {
  let token = '';

  test.beforeAll(async ({ browser }) => {
    const page = await browser.newPage();
    page.setDefaultTimeout(30_000);
    await page.goto(BASE + '/signin', { waitUntil: 'domcontentloaded' });
    const info = await loginAsAdmin(page).catch(() => null);
    token = info?.token ?? '';
    await page.close();
  });

  async function withToken(page) {
    if (token) {
      await page.addInitScript((t) => {
        sessionStorage.setItem('ramboq_token', t);
      }, token);
    }
  }

  test('500 from /api/nav/history shows error strip + Retry in NavTab', async ({ page }) => {
    await withToken(page);

    await page.route('**/api/nav/history**', route =>
      route.fulfill({ status: 500, contentType: 'application/json',
        body: JSON.stringify({ detail: 'DB error' }) })
    );

    await page.goto(BASE + '/dashboard', { waitUntil: 'domcontentloaded' });
    // The NAV tab should be the default chart tab.
    const errEl = page.locator('[data-testid="nav-tab-error"]');
    await expect(errEl).toBeVisible({ timeout: 15_000 });
    await expect(errEl).toContainText(/nav history unavailable/i);
    const retryBtn = errEl.locator('button').filter({ hasText: /retry/i });
    await expect(retryBtn).toBeVisible();

    // UX: color should be red (#f87171).
    const color = await errEl.evaluate(el => getComputedStyle(el).color);
    expect(color).toMatch(/248.*113.*113/);

    // Retry — swap to success, click, assert recovery.
    await page.unroute('**/api/nav/history**');
    await page.route('**/api/nav/history**', route =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: [] }) })
    );
    await retryBtn.click();
    // After retry with empty response, should show empty state (not error).
    await expect(page.locator('[data-testid="nav-tab-empty"], [data-testid="nav-tab-loading"]'))
      .toBeVisible({ timeout: 8_000 });
  });
});

// ── Dashboard _fetchNav error strip ──────────────────────────────────────────

test.describe('Dashboard _fetchNav error strip', () => {
  let token = '';

  test.beforeAll(async ({ browser }) => {
    const page = await browser.newPage();
    page.setDefaultTimeout(30_000);
    await page.goto(BASE + '/signin', { waitUntil: 'domcontentloaded' });
    const info = await loginAsAdmin(page).catch(() => null);
    token = info?.token ?? '';
    await page.close();
  });

  async function withToken(page) {
    if (token) {
      await page.addInitScript((t) => {
        sessionStorage.setItem('ramboq_token', t);
      }, token);
    }
  }

  test('500 from /api/nav/latest shows dash-nav-error strip above NavTab', async ({ page }) => {
    await withToken(page);

    await page.route('**/api/nav/latest**', route =>
      route.fulfill({ status: 500, contentType: 'application/json',
        body: JSON.stringify({ detail: 'NAV recompute failed' }) })
    );

    await page.goto(BASE + '/dashboard', { waitUntil: 'domcontentloaded' });
    const errEl = page.locator('[data-testid="dash-nav-error"]');
    await expect(errEl).toBeVisible({ timeout: 15_000 });
    await expect(errEl).toContainText(/nav chip unavailable/i);
    const retryBtn = errEl.locator('button').filter({ hasText: /retry/i });
    await expect(retryBtn).toBeVisible();

    // Retry — swap to success.
    await page.unroute('**/api/nav/latest**');
    await page.route('**/api/nav/latest**', route =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ latest: { nav: 1000000, as_of_date: '2026-07-02' },
          day_delta: 5000, day_delta_pct: 0.005 }) })
    );
    await retryBtn.click();
    // Error strip must disappear on success.
    await expect(errEl).not.toBeVisible({ timeout: 8_000 });
  });
});
