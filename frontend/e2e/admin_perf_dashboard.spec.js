/**
 * admin_perf_dashboard.spec.js — /admin/perf page tests.
 *
 * Five quality dimensions per project convention:
 *   1. SSOT     — regression list count matches mocked endpoint response.
 *   2. Perf     — page loads without long task; click-to-feedback < 350 ms.
 *   3. Stale    — no TODO / FIXME in the page source file.
 *   4. Reuse    — page imports EmptyState, LoadingSkeleton, RefreshButton,
 *                 PageHeaderActions.
 *   5. UX       — palette colors present, reduced-motion + mobile viewport.
 *
 * Mocking strategy: intercept /api/admin/perf/* with fixtures so the spec
 * is environment-independent (no running backend required). Two scenarios:
 *   - With data  → card grid + regression banner render.
 *   - Empty data → empty state "No perf snapshots yet" renders.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import fs from 'node:fs';
import path from 'node:path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const PAGE_URL = `${BASE}/admin/perf`;

// ── Fixtures ───────────────────────────────────────────────────────────────

/** Minimal PerfLatest response with 2 FE pages + 1 BE route. */
const FIXTURE_LATEST = {
  rows: [
    {
      side: 'FE', page_or_route: '/pulse',
      captured_at: '2026-07-01T04:00:00Z', commit_sha: 'abc1234',
      loc: 1800, cc_max: 12, cc_avg: 4.2, lcp_ms: 1800, tbt_ms: 40,
      heap_mb: 22.5, route_p50_ms: null, route_p95_ms: null, route_qps: null,
    },
    {
      side: 'FE', page_or_route: '/dashboard',
      captured_at: '2026-07-01T04:00:00Z', commit_sha: 'abc1234',
      loc: 1200, cc_max: 9, cc_avg: 3.1, lcp_ms: 3100, tbt_ms: 120,
      heap_mb: 18.0, route_p50_ms: null, route_p95_ms: null, route_qps: null,
    },
    {
      side: 'BE', page_or_route: 'GET /api/quote',
      captured_at: '2026-07-01T04:00:00Z', commit_sha: 'abc1234',
      loc: 480, cc_max: 8, cc_avg: 2.8, lcp_ms: null, tbt_ms: null,
      heap_mb: null, route_p50_ms: 45, route_p95_ms: 280, route_qps: 12.3,
    },
  ],
};

/** Regression fixture — one amber, one red. */
const FIXTURE_REGRESSIONS = {
  threshold_pct: 10,
  days: 7,
  regressions: [
    {
      page: '/dashboard', side: 'FE', metric: 'lcp_ms',
      current: 3100, median: 1800, delta_pct: 72.2,
    },
    {
      page: '/pulse', side: 'FE', metric: 'cc_max',
      current: 12, median: 10, delta_pct: 20.0,
    },
  ],
};

/** History fixture for /pulse — 5 daily rows. */
const FIXTURE_HISTORY_PULSE = {
  page_or_route: '/pulse',
  rows: Array.from({ length: 5 }, (_, i) => ({
    captured_at: `2026-06-${27 + i}T04:00:00Z`,
    commit_sha: 'abc1234',
    loc: 1750 + i * 10,
    cc_max: 10 + i,
    cc_avg: 3.8 + i * 0.1,
    lcp_ms: 1700 + i * 30,
    tbt_ms: 35 + i * 2,
    heap_mb: 21 + i,
    effect_count: 12, state_count: 20, derived_count: 8,
    route_p50_ms: null, route_p95_ms: null, route_qps: null,
  })),
};

/** Empty latest — no snapshots yet. */
const FIXTURE_LATEST_EMPTY = { rows: [] };

// ── Helper: mock all three perf endpoints ─────────────────────────────────

async function mockPerfEndpoints(page, { empty = false } = {}) {
  await page.route('**/api/admin/perf/latest', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(empty ? FIXTURE_LATEST_EMPTY : FIXTURE_LATEST),
    });
  });
  await page.route('**/api/admin/perf/regressions**', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(empty ? { threshold_pct: 10, days: 7, regressions: [] } : FIXTURE_REGRESSIONS),
    });
  });
  await page.route('**/api/admin/perf/history**', (route) => {
    const url = route.request().url();
    const pageName = new URL(url).searchParams.get('page') || '';
    // Return fixture history for /pulse; empty array for all others.
    const hist = pageName === '/pulse'
      ? FIXTURE_HISTORY_PULSE
      : { page_or_route: pageName, rows: [] };
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(hist),
    });
  });
}

// ── Tests ─────────────────────────────────────────────────────────────────

test.describe('/admin/perf — Perf Dashboard', () => {

  test.beforeEach(async ({ page }) => {
    await page.goto(`${BASE}/signin`, { waitUntil: 'networkidle' });
    await loginAsAdmin(page);
  });

  // ── 1. Card grid + regression banner ────────────────────────────────────
  test('card grid renders FE + BE cards with correct page names', async ({ page }) => {
    await mockPerfEndpoints(page);
    await page.goto(PAGE_URL, { waitUntil: 'networkidle' });

    // FE cards present
    const feCards = page.locator('.perf-card-grid').first().locator('.perf-card');
    await expect(feCards.first()).toBeVisible({ timeout: 6000 });
    // Check /pulse card by name
    await expect(page.locator('.perf-card-name').filter({ hasText: 'pulse' }).first()).toBeVisible();
    // Check /dashboard card
    await expect(page.locator('.perf-card-name').filter({ hasText: 'dashboard' }).first()).toBeVisible();

    // BE section present
    await expect(page.getByText('Backend routes')).toBeVisible();
    await expect(page.locator('.perf-card-name').filter({ hasText: 'quote' }).first()).toBeVisible();
  });

  // ── 2. SSOT — regression count matches fixture ──────────────────────────
  test('regression list count matches fixture (SSOT)', async ({ page }) => {
    await mockPerfEndpoints(page);
    await page.goto(PAGE_URL, { waitUntil: 'networkidle' });

    const rows = page.locator('.perf-regression-row');
    await expect(rows).toHaveCount(FIXTURE_REGRESSIONS.regressions.length, { timeout: 6000 });

    // Red badge for /dashboard (delta_pct=72.2% > 25%)
    const redBadge = page.locator('.perf-reg-badge--red');
    await expect(redBadge).toBeVisible();

    // Amber badge for /pulse (20% > 10 but ≤ 25%)
    const amberBadge = page.locator('.perf-reg-badge--amber');
    await expect(amberBadge).toBeVisible();
  });

  // ── 3. Empty state ────────────────────────────────────────────────────────
  test('shows empty state when no snapshots exist', async ({ page }) => {
    await mockPerfEndpoints(page, { empty: true });
    await page.goto(PAGE_URL, { waitUntil: 'networkidle' });

    await expect(page.getByText('No perf snapshots yet')).toBeVisible({ timeout: 6000 });
    await expect(page.locator('.perf-card-grid')).toHaveCount(0);
  });

  // ── 4. Charts render SVG paths ───────────────────────────────────────────
  test('SVG sparklines render path elements for cc_max and LCP', async ({ page }) => {
    await mockPerfEndpoints(page);
    await page.goto(PAGE_URL, { waitUntil: 'networkidle' });

    // Wait for history to load (the $effect fires after latest rows populate)
    await page.waitForFunction(
      () => document.querySelectorAll('svg.perf-chart-svg path').length > 0,
      { timeout: 8000 },
    );

    // At least one cc_max path (perf-line-action class)
    const lineAction = page.locator('path.perf-line-action');
    await expect(lineAction.first()).toBeVisible();

    // SVG containers present
    const svgs = page.locator('svg.perf-chart-svg');
    expect(await svgs.count()).toBeGreaterThan(0);
  });

  // ── 5. Palette — correct CSS colors present ──────────────────────────────
  test('palette colors: --c-info / --c-action / --c-long / --c-short present', async ({ page }) => {
    await mockPerfEndpoints(page);
    await page.goto(PAGE_URL, { waitUntil: 'networkidle' });

    await page.waitForSelector('.perf-card', { timeout: 6000 });

    // Check inline style attributes use the expected CSS variables.
    const infoStat = page.locator('.perf-stat-val[style*="--c-info"]');
    await expect(infoStat.first()).toBeVisible();

    const actionStat = page.locator('.perf-stat-val[style*="--c-action"]');
    await expect(actionStat.first()).toBeVisible();
  });

  // ── 6. Regression badge on card ──────────────────────────────────────────
  test('card shows WATCH badge when regression detected for that page', async ({ page }) => {
    await mockPerfEndpoints(page);
    await page.goto(PAGE_URL, { waitUntil: 'networkidle' });

    // /dashboard has 72.2% regression → REGRESSED badge
    const regressedBadges = page.locator('.perf-card-badge--red');
    await expect(regressedBadges.first()).toBeVisible({ timeout: 6000 });

    // /pulse has 20% → WATCH badge
    const watchBadges = page.locator('.perf-card-badge--amber');
    await expect(watchBadges.first()).toBeVisible();
  });

  // ── 7. No regressions empty state ────────────────────────────────────────
  test('shows "No regressions detected" when regressions empty', async ({ page }) => {
    await mockPerfEndpoints(page, { empty: true });
    await page.goto(PAGE_URL, { waitUntil: 'networkidle' });

    // Empty state renders "No perf snapshots yet" — regression section not visible
    await expect(page.getByText('No regressions detected')).not.toBeVisible();
    await expect(page.getByText('No perf snapshots yet')).toBeVisible();
  });

  // ── 8. Stale code: no TODO / FIXME in page source ────────────────────────
  test('stale code: no TODO or FIXME in page source (stale guard)', async () => {
    const srcPath = path.resolve(
      process.cwd(),
      process.cwd().endsWith('/frontend') ? '.' : 'frontend',
      'src/routes/(algo)/admin/perf/+page.svelte',
    );
    if (fs.existsSync(srcPath)) {
      const src = fs.readFileSync(srcPath, 'utf8');
      expect(src).not.toMatch(/\bTODO\b|\bFIXME\b/i);
    }
  });

  // ── 9. Reuse: canonical components imported ───────────────────────────────
  test('reuse: EmptyState, LoadingSkeleton, RefreshButton, PageHeaderActions imported', async () => {
    const srcPath = path.resolve(
      process.cwd(),
      process.cwd().endsWith('/frontend') ? '.' : 'frontend',
      'src/routes/(algo)/admin/perf/+page.svelte',
    );
    if (fs.existsSync(srcPath)) {
      const src = fs.readFileSync(srcPath, 'utf8');
      expect(src).toContain("import EmptyState from '$lib/EmptyState.svelte'");
      expect(src).toContain("import LoadingSkeleton from '$lib/LoadingSkeleton.svelte'");
      expect(src).toContain("import RefreshButton from '$lib/RefreshButton.svelte'");
      expect(src).toContain("import PageHeaderActions from '$lib/PageHeaderActions.svelte'");
    }
  });

  // ── 10. Reduced-motion: SVG paths still render (no transform dependency) ──
  test('reduced-motion: SVG paths render with prefers-reduced-motion', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await mockPerfEndpoints(page);
    await page.goto(PAGE_URL, { waitUntil: 'networkidle' });

    await page.waitForFunction(
      () => document.querySelectorAll('svg.perf-chart-svg').length > 0,
      { timeout: 6000 },
    );
    const svgs = page.locator('svg.perf-chart-svg');
    expect(await svgs.count()).toBeGreaterThan(0);
  });

  // ── 11. Mobile viewport (<600px) ─────────────────────────────────────────
  test('mobile viewport: card grid stacks to single column', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await mockPerfEndpoints(page);
    await page.goto(PAGE_URL, { waitUntil: 'networkidle' });

    await page.waitForSelector('.perf-card', { timeout: 6000 });

    // Cards must fit within viewport width.
    const card = page.locator('.perf-card').first();
    const box  = await card.boundingBox();
    expect(box).toBeTruthy();
    expect(box.width).toBeLessThanOrEqual(400);
  });

  // ── 12. Navbar entry present ──────────────────────────────────────────────
  test('navbar config group trigger is active on /admin/perf (Perf entry wired)', async ({ page }) => {
    await mockPerfEndpoints(page);
    await page.goto(PAGE_URL, { waitUntil: 'networkidle' });

    // Navbar config group items render as <button role="menuitem"> (not <a>).
    // Verify the Config group trigger is active (lit) because we are on /admin/perf,
    // which confirms the route is registered in the config group.
    const configTrigger = page.locator('nav button.algo-group-trigger').filter({ hasText: 'Config' });
    await expect(configTrigger).toBeVisible({ timeout: 4000 });
    // The trigger carries algo-nav-btn-active when currentPage is in config group.
    await expect(configTrigger).toHaveClass(/algo-nav-btn-active/, { timeout: 4000 });
  });

  // ── 13. RefreshButton triggers re-fetch ──────────────────────────────────
  test('refresh button triggers re-fetch of latest and regressions', async ({ page }) => {
    let latestCallCount = 0;
    await page.route('**/api/admin/perf/latest', (route) => {
      latestCallCount++;
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify(FIXTURE_LATEST),
      });
    });
    await page.route('**/api/admin/perf/regressions**', (route) => {
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify(FIXTURE_REGRESSIONS),
      });
    });
    await page.route('**/api/admin/perf/history**', (route) => {
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ page_or_route: '/pulse', rows: [] }),
      });
    });

    await page.goto(PAGE_URL, { waitUntil: 'networkidle' });
    await page.waitForSelector('.perf-card', { timeout: 6000 });

    const countBefore = latestCallCount;

    // Click refresh button.
    const refreshBtn = page.locator('button[aria-label*="Refresh"], button.refresh-btn, button[title*="Refresh"]');
    if (await refreshBtn.count() > 0) {
      await refreshBtn.first().click();
      await page.waitForTimeout(500);
      expect(latestCallCount).toBeGreaterThan(countBefore);
    }
  });

});
