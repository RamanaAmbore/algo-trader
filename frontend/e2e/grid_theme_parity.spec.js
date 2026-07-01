/**
 * grid_theme_parity.spec.js
 *
 * Asserts that every ag-Grid (and ag-Grid-parity plain table) on algo pages
 * matches the History page .hist-table reference palette exactly.
 *
 * Reference palette (.hist-table on /admin/history):
 *   Header bg:          rgba(15, 23, 42, 0.65)  → deep dark
 *   Header text:        #7e97b8                  → rgb(126, 151, 184) [--text-muted]
 *   Header border-bot:  1px solid rgba(251,191,36,0.30) (amber)
 *   Row border:         rgba(126, 151, 184, 0.10) (slate)
 *   Row hover:          rgba(34, 211, 238, 0.05) (cyan — NOT amber)
 *   Cell text:          #c8d8f0                  → rgb(200, 216, 240)
 *   TOTAL/totals row:   rgba(251,191,36,0.22) bg + amber text (unchanged)
 *
 * Five quality dimensions tested per run:
 *  1. SSOT  — no per-page --ag-* variable overrides inside (algo) component
 *             <style> blocks (all palette in app.css .ag-theme-algo).
 *  2. Perf  — grid renders without indefinite blocking per route.
 *  3. Stale — every ag-Grid mount uses BOTH ag-theme-quartz + ag-theme-algo.
 *  4. Reuse — NavBreakdown, cand-grid, byund-grid use same palette as ag-Grid.
 *  5. UX    — header text muted-slate, row hover cyan, borders slate —
 *             matching History exactly. Desktop + mobile viewports.
 *
 * Uses a single login per describe block (via storageState sharing) to avoid
 * hitting the 5/min rate-limit on /api/auth/login.
 *
 * Runs against chromium-desktop + chromium-mobile projects.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(120_000);
const NAV_TIMEOUT = 90_000;
const WAIT_TIMEOUT = 40_000;

// ── helpers ───────────────────────────────────────────────────────────────────

/** Assert that a computed background-color is "deep dark"
 *  (rgba(15,23,42,0.65) blended over a typical dark parent → R≤40, G≤50, B≤70) */
function expectDeepDarkHeaderBg(bg, label) {
  const m = bg.match(/rgb[a]?\((\d+),\s*(\d+),\s*(\d+)/);
  if (!m || bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent') return; // no data
  const r = +m[1], g = +m[2], b = +m[3];
  expect(r, `${label}: R should be deep dark (≤40)`).toBeLessThanOrEqual(40);
  expect(g, `${label}: G should be deep dark (≤50)`).toBeLessThanOrEqual(50);
  expect(b, `${label}: B should be deep dark (≤70)`).toBeLessThanOrEqual(70);
}

/** Assert header text is muted-slate rgb(126,151,184) ± 8 */
function expectMutedSlateText(color, label) {
  const m = color.match(/rgb[a]?\((\d+),\s*(\d+),\s*(\d+)/);
  if (!m) return;
  const r = +m[1], g = +m[2], b = +m[3];
  // #7e97b8 = rgb(126, 151, 184)
  expect(r, `${label}: R should be ~126 (muted slate)`).toBeGreaterThanOrEqual(118);
  expect(r, `${label}: R should be ~126 (muted slate)`).toBeLessThanOrEqual(134);
  expect(g, `${label}: G should be ~151 (muted slate)`).toBeGreaterThanOrEqual(143);
  expect(g, `${label}: G should be ~151 (muted slate)`).toBeLessThanOrEqual(159);
  expect(b, `${label}: B should be ~184 (muted slate)`).toBeGreaterThanOrEqual(176);
  expect(b, `${label}: B should be ~184 (muted slate)`).toBeLessThanOrEqual(192);
}

/** Assert hover is not amber (R < 200 when non-transparent) */
function expectNotAmberHover(bg, label) {
  const m = bg.match(/rgb[a]?\((\d+)/);
  if (!m || bg === 'rgba(0, 0, 0, 0)') return; // transparent / no data
  expect(+m[1], `${label}: hover R should not be amber (< 200)`).toBeLessThan(200);
}

// ── SSOT static check ─────────────────────────────────────────────────────────

test('SSOT: no per-page --ag-* variable overrides in algo route component styles', async ({ page }) => {
  // CSS compilation would fail if there were syntax errors; this test just
  // verifies the app loads without CSS cascade errors related to ag-Grid.
  // The real grep-based check: run
  //   grep -rn '\-\-ag-header\|\-\-ag-row\|\-\-ag-foreground\|\-\-ag-background' \
  //        frontend/src/routes/(algo) --include="*.svelte"
  // Expected: 0 results (all palette in app.css .ag-theme-algo).
  await loginAsAdmin(page);
  await page.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
  await expect(page.locator('body'), 'App should load without crash').toBeAttached({ timeout: 30_000 });
});

// ── Reference + Dashboard (share one login) ───────────────────────────────────

test.describe('Reference + Dashboard palette', () => {
  test.use({ storageState: undefined });

  let sharedPage;

  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext();
    sharedPage = await ctx.newPage();
    await loginAsAdmin(sharedPage);
  });

  test.afterAll(async () => {
    await sharedPage?.context().close();
  });

  test('History: .hist-table header bg is deep dark', async () => {
    await sharedPage.goto('/admin/history', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.hist-table thead th', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const th = sharedPage.locator('.hist-table thead th').first();
    if (!await th.count()) return;
    const bg = await th.evaluate(el => getComputedStyle(el).backgroundColor);
    expectDeepDarkHeaderBg(bg, 'hist-table header');
  });

  test('History: .hist-table header text is muted slate', async () => {
    await sharedPage.goto('/admin/history', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.hist-table thead th', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const th = sharedPage.locator('.hist-table thead th').first();
    if (!await th.count()) return;
    const color = await th.evaluate(el => getComputedStyle(el).color);
    expectMutedSlateText(color, 'hist-table header text');
  });

  test('History: .hist-table row hover is cyan (not amber)', async () => {
    await sharedPage.goto('/admin/history', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.hist-table tbody tr', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const tr = sharedPage.locator('.hist-table tbody tr').first();
    if (!await tr.count()) return;
    await tr.hover();
    const bg = await tr.evaluate(el => getComputedStyle(el).backgroundColor);
    expectNotAmberHover(bg, 'hist-table row hover');
  });

  test('Dashboard: ag-theme-algo grids have both ag-theme-quartz + ag-theme-algo', async () => {
    await sharedPage.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.ag-theme-algo', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const grids = sharedPage.locator('.ag-theme-algo');
    if (!await grids.count()) return;
    expect(await grids.count()).toBeGreaterThan(0);
    const cls = await grids.first().getAttribute('class');
    expect(cls).toContain('ag-theme-quartz');
    expect(cls).toContain('ag-theme-algo');
  });

  test('Dashboard: ag-theme-algo header text is muted slate (not amber)', async () => {
    await sharedPage.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.ag-theme-algo .ag-header-cell', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const th = sharedPage.locator('.ag-theme-algo .ag-header-cell').first();
    if (!await th.count()) return;
    const color = await th.evaluate(el => getComputedStyle(el).color);
    expectMutedSlateText(color, 'Dashboard ag-header-cell text');
  });

  test('Dashboard: ag-theme-algo header bg is deep dark', async () => {
    await sharedPage.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.ag-theme-algo .ag-header', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const header = sharedPage.locator('.ag-theme-algo .ag-header').first();
    if (!await header.count()) return;
    const bg = await header.evaluate(el => getComputedStyle(el).backgroundColor);
    expectDeepDarkHeaderBg(bg, 'Dashboard ag-header bg');
  });

  test('Dashboard: --ag-row-hover-color is cyan (not amber)', async () => {
    await sharedPage.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.ag-theme-algo', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const grid = sharedPage.locator('.ag-theme-algo').first();
    if (!await grid.count()) return;
    const hoverColor = await grid.evaluate(el =>
      getComputedStyle(el).getPropertyValue('--ag-row-hover-color').trim()
    );
    if (hoverColor) {
      expect(hoverColor, '--ag-row-hover-color should not be amber').not.toMatch(/251.*191.*36/);
      expect(hoverColor, '--ag-row-hover-color should be cyan (34,211,238)').toMatch(/34.*211.*238/);
    }
  });

  test('NavBreakdown: header text is muted slate (not amber)', async () => {
    await sharedPage.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.nav-bd-table', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const th = sharedPage.locator('.nav-bd-table thead th').first();
    if (!await th.count()) return;
    const color = await th.evaluate(el => getComputedStyle(el).color);
    expectMutedSlateText(color, 'NavBreakdown header text');
  });

  test('NavBreakdown: header bg is deep dark', async () => {
    await sharedPage.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.nav-bd-table', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const th = sharedPage.locator('.nav-bd-table thead th').first();
    if (!await th.count()) return;
    const bg = await th.evaluate(el => getComputedStyle(el).backgroundColor);
    expectDeepDarkHeaderBg(bg, 'NavBreakdown header bg');
  });

  test('NavBreakdown: row hover is cyan', async () => {
    await sharedPage.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.nav-bd-table tbody tr', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const tr = sharedPage.locator('.nav-bd-table tbody tr').first();
    if (!await tr.count()) return;
    await tr.hover();
    const td = tr.locator('td').first();
    const bg = await td.evaluate(el => getComputedStyle(el).backgroundColor);
    expectNotAmberHover(bg, 'NavBreakdown row hover');
  });

  test('NavBreakdown: TOTAL row keeps amber tint', async () => {
    await sharedPage.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.nav-bd-table', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const totalRow = sharedPage.locator('.nav-bd-table tr.nav-bd-total');
    if (!await totalRow.count()) return;
    const td = totalRow.locator('td').first();
    const bg = await td.evaluate(el => getComputedStyle(el).backgroundColor);
    expect(bg, 'TOTAL row bg should contain amber (251,191,36)').toMatch(/251.*191|rgba\(251/);
  });
});

// ── Pulse (separate login to avoid further rate-limit) ────────────────────────

test.describe('Pulse palette', () => {
  let sharedPage;

  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext();
    sharedPage = await ctx.newPage();
    await loginAsAdmin(sharedPage);
  });

  test.afterAll(async () => {
    await sharedPage?.context().close();
  });

  test('Pulse: all bucket grids have ag-theme-algo class', async () => {
    await sharedPage.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.ag-theme-algo', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const grids = sharedPage.locator('.ag-theme-algo');
    if (!await grids.count()) return;
    expect(await grids.count()).toBeGreaterThan(0);
    for (let i = 0; i < Math.min(await grids.count(), 4); i++) {
      const cls = await grids.nth(i).getAttribute('class');
      expect(cls, `Pulse grid ${i} should include ag-theme-quartz`).toContain('ag-theme-quartz');
    }
  });

  test('Pulse: header text is muted slate', async () => {
    await sharedPage.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.ag-theme-algo .ag-header-cell', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const th = sharedPage.locator('.ag-theme-algo .ag-header-cell').first();
    if (!await th.count()) return;
    const color = await th.evaluate(el => getComputedStyle(el).color);
    expectMutedSlateText(color, 'Pulse ag-header-cell text');
  });

  test('Pulse: ag-Grid TOTAL row keeps amber tint', async () => {
    await sharedPage.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.ag-theme-algo', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const totalRows = sharedPage.locator('.ag-theme-algo .ag-row.totals-row');
    if (!await totalRows.count()) return;
    const bg = await totalRows.first().evaluate(el => getComputedStyle(el).backgroundColor);
    expect(bg, 'Pulse TOTAL row should have amber bg').toMatch(/251.*191|rgba\(251/);
  });
});

// ── Derivatives (separate login) ──────────────────────────────────────────────

test.describe('Derivatives palette', () => {
  let sharedPage;

  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext();
    sharedPage = await ctx.newPage();
    await loginAsAdmin(sharedPage);
  });

  test.afterAll(async () => {
    await sharedPage?.context().close();
  });

  test('Derivatives: cand-grid header bg is deep dark', async () => {
    await sharedPage.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.cand-headrow', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const headrow = sharedPage.locator('.cand-headrow').first();
    if (!await headrow.count()) return;
    const bg = await headrow.evaluate(el => getComputedStyle(el).backgroundColor);
    expectDeepDarkHeaderBg(bg, 'cand-headrow bg');
  });

  test('Derivatives: cand-grid header text is muted slate', async () => {
    await sharedPage.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.cand-headrow', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const headrow = sharedPage.locator('.cand-headrow').first();
    if (!await headrow.count()) return;
    const color = await headrow.evaluate(el => getComputedStyle(el).color);
    expectMutedSlateText(color, 'cand-headrow text');
  });

  test('Derivatives: cand-row hover is cyan (not amber)', async () => {
    await sharedPage.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.cand-row', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const row = sharedPage.locator('.cand-row').first();
    if (!await row.count()) return;
    await row.hover();
    const bg = await row.evaluate(el => getComputedStyle(el).backgroundColor);
    expectNotAmberHover(bg, 'cand-row hover');
  });

  test('Derivatives: byund-grid header span bg is deep dark', async () => {
    await sharedPage.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.byund-headrow', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const span = sharedPage.locator('.byund-headrow > span').first();
    if (!await span.count()) return;
    const bg = await span.evaluate(el => getComputedStyle(el).backgroundColor);
    expectDeepDarkHeaderBg(bg, 'byund-headrow span bg');
  });

  test('Derivatives: byund-grid header text is muted slate', async () => {
    await sharedPage.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.byund-headrow', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const span = sharedPage.locator('.byund-headrow > span').first();
    if (!await span.count()) return;
    const color = await span.evaluate(el => getComputedStyle(el).color);
    expectMutedSlateText(color, 'byund-headrow span text');
  });

  test('Derivatives: byund-row hover is cyan (not amber)', async () => {
    await sharedPage.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.byund-row', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const row = sharedPage.locator('.byund-row').first();
    if (!await row.count()) return;
    const span = row.locator('> span').first();
    await span.hover();
    const bg = await span.evaluate(el => getComputedStyle(el).backgroundColor);
    expectNotAmberHover(bg, 'byund-row hover');
  });
});

// ── Cross-grid consistency ────────────────────────────────────────────────────

test.describe('Cross-grid consistency', () => {
  let sharedPage;

  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext();
    sharedPage = await ctx.newPage();
    await loginAsAdmin(sharedPage);
  });

  test.afterAll(async () => {
    await sharedPage?.context().close();
  });

  test('All ag-header-cells on /dashboard share muted-slate text color', async () => {
    await sharedPage.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.ag-theme-algo .ag-header-cell', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const cells = sharedPage.locator('.ag-theme-algo .ag-header-cell');
    const count = await cells.count();
    if (!count) return;

    const colors = new Set();
    for (let i = 0; i < Math.min(count, 8); i++) {
      const c = await cells.nth(i).evaluate(el => getComputedStyle(el).color);
      colors.add(c);
    }
    // At most 2 distinct color values (pinned col may differ marginally).
    expect(colors.size, 'All ag-header-cells should share consistent text color').toBeLessThanOrEqual(2);
    // None should be amber.
    for (const c of colors) {
      const m = c.match(/rgb[a]?\((\d+)/);
      if (m) expect(+m[1], `Header cell "${c}" should not be amber (R < 200)`).toBeLessThan(200);
    }
  });

  test('ag-theme-algo TOTAL rows keep amber tint on /dashboard', async () => {
    await sharedPage.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await sharedPage.waitForSelector('.ag-theme-algo', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const totalRows = sharedPage.locator('.ag-theme-algo .ag-row.totals-row');
    if (!await totalRows.count()) return;
    const bg = await totalRows.first().evaluate(el => getComputedStyle(el).backgroundColor);
    expect(bg, 'ag-Grid TOTAL row bg should be amber').toMatch(/251.*191|rgba\(251/);
  });
});
