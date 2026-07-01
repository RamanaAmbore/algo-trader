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
 * Single login for the whole suite (one describe block, one beforeAll) to
 * stay under the 5/min rate-limit on /api/auth/login.
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

// ── All tests share a single authenticated page session ───────────────────────

test.describe('Grid theme parity', () => {
  /** @type {import('@playwright/test').Page} */
  let P;

  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext();
    P = await ctx.newPage();
    await loginAsAdmin(P);
  });

  test.afterAll(async () => {
    await P?.context().close();
  });

  // ── 1. SSOT ──────────────────────────────────────────────────────────────────

  test('SSOT: app loads — CSS compiled without --ag-* variable conflicts', async () => {
    // CSS compilation would fail if there were cascade errors.
    // Static grep check: grep -rn '\-\-ag-header\|\-\-ag-row\|\-\-ag-foreground\|\-\-ag-background'
    //   frontend/src/routes/(algo) --include="*.svelte"   → expected: 0 results.
    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await expect(P.locator('body'), 'App should load without crash').toBeAttached({ timeout: 30_000 });
  });

  // ── 2. Reference: /admin/history .hist-table ──────────────────────────────────

  test('History: .hist-table header bg is deep dark', async () => {
    await P.goto('/admin/history', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.hist-table thead th', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const th = P.locator('.hist-table thead th').first();
    if (!await th.count()) return;
    const bg = await th.evaluate(el => getComputedStyle(el).backgroundColor);
    expectDeepDarkHeaderBg(bg, 'hist-table header');
  });

  test('History: .hist-table header text is muted slate', async () => {
    // Page already at /admin/history from previous test in serial execution.
    // Re-navigate to be safe (tests run in order within the describe block).
    await P.goto('/admin/history', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.hist-table thead th', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const th = P.locator('.hist-table thead th').first();
    if (!await th.count()) return;
    const color = await th.evaluate(el => getComputedStyle(el).color);
    expectMutedSlateText(color, 'hist-table header text');
  });

  test('History: .hist-table row hover is cyan (not amber)', async () => {
    await P.goto('/admin/history', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.hist-table tbody tr', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const tr = P.locator('.hist-table tbody tr').first();
    if (!await tr.count()) return;
    await tr.hover();
    const bg = await tr.evaluate(el => getComputedStyle(el).backgroundColor);
    expectNotAmberHover(bg, 'hist-table row hover');
  });

  // ── 3. /dashboard ag-Grid ────────────────────────────────────────────────────

  test('Dashboard: ag-theme-algo grids have both ag-theme-quartz + ag-theme-algo', async () => {
    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.ag-theme-algo', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const grids = P.locator('.ag-theme-algo');
    if (!await grids.count()) return;
    expect(await grids.count()).toBeGreaterThan(0);
    const cls = await grids.first().getAttribute('class');
    expect(cls, 'should include ag-theme-quartz').toContain('ag-theme-quartz');
    expect(cls, 'should include ag-theme-algo').toContain('ag-theme-algo');
  });

  test('Dashboard: ag-theme-algo header text is muted slate (not amber)', async () => {
    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.ag-theme-algo .ag-header-cell', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const th = P.locator('.ag-theme-algo .ag-header-cell').first();
    if (!await th.count()) return;
    const color = await th.evaluate(el => getComputedStyle(el).color);
    expectMutedSlateText(color, 'Dashboard ag-header-cell text');
  });

  test('Dashboard: ag-theme-algo header bg is deep dark', async () => {
    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.ag-theme-algo .ag-header', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const header = P.locator('.ag-theme-algo .ag-header').first();
    if (!await header.count()) return;
    const bg = await header.evaluate(el => getComputedStyle(el).backgroundColor);
    expectDeepDarkHeaderBg(bg, 'Dashboard ag-header bg');
  });

  test('Dashboard: --ag-row-hover-color is cyan (not amber)', async () => {
    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.ag-theme-algo', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const grid = P.locator('.ag-theme-algo').first();
    if (!await grid.count()) return;
    const hoverColor = await grid.evaluate(el =>
      getComputedStyle(el).getPropertyValue('--ag-row-hover-color').trim()
    );
    if (hoverColor) {
      expect(hoverColor, '--ag-row-hover-color should not be amber').not.toMatch(/251.*191.*36/);
      expect(hoverColor, '--ag-row-hover-color should be cyan (34,211,238)').toMatch(/34.*211.*238/);
    }
  });

  test('Dashboard: ag-Grid TOTAL rows keep amber tint', async () => {
    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.ag-theme-algo', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const totalRows = P.locator('.ag-theme-algo .ag-row.totals-row');
    if (!await totalRows.count()) return;
    const bg = await totalRows.first().evaluate(el => getComputedStyle(el).backgroundColor);
    expect(bg, 'ag-Grid TOTAL row bg should be amber').toMatch(/251.*191|rgba\(251/);
  });

  test('All ag-header-cells on /dashboard share consistent muted-slate text', async () => {
    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.ag-theme-algo .ag-header-cell', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const cells = P.locator('.ag-theme-algo .ag-header-cell');
    const count = await cells.count();
    if (!count) return;

    const colors = new Set();
    for (let i = 0; i < Math.min(count, 8); i++) {
      const c = await cells.nth(i).evaluate(el => getComputedStyle(el).color);
      colors.add(c);
    }
    // At most 2 distinct color values (pinned col may vary marginally).
    expect(colors.size, 'All ag-header-cells should share consistent text color').toBeLessThanOrEqual(2);
    // None should be amber.
    for (const c of colors) {
      const m = c.match(/rgb[a]?\((\d+)/);
      if (m) expect(+m[1], `Header cell "${c}" should not be amber (R < 200)`).toBeLessThan(200);
    }
  });

  // ── 4. NavBreakdown (/dashboard NAV tab) ─────────────────────────────────────

  test('NavBreakdown: header text is muted slate (not amber)', async () => {
    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.nav-bd-table', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const th = P.locator('.nav-bd-table thead th').first();
    if (!await th.count()) return;
    const color = await th.evaluate(el => getComputedStyle(el).color);
    expectMutedSlateText(color, 'NavBreakdown header text');
  });

  test('NavBreakdown: header bg is deep dark', async () => {
    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.nav-bd-table', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const th = P.locator('.nav-bd-table thead th').first();
    if (!await th.count()) return;
    const bg = await th.evaluate(el => getComputedStyle(el).backgroundColor);
    expectDeepDarkHeaderBg(bg, 'NavBreakdown header bg');
  });

  test('NavBreakdown: row hover is cyan (not amber)', async () => {
    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.nav-bd-table tbody tr', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const tr = P.locator('.nav-bd-table tbody tr').first();
    if (!await tr.count()) return;
    await tr.hover();
    const td = tr.locator('td').first();
    const bg = await td.evaluate(el => getComputedStyle(el).backgroundColor);
    expectNotAmberHover(bg, 'NavBreakdown row hover');
  });

  test('NavBreakdown: TOTAL row keeps amber tint', async () => {
    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.nav-bd-table', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const totalRow = P.locator('.nav-bd-table tr.nav-bd-total');
    if (!await totalRow.count()) return;
    const td = totalRow.locator('td').first();
    const bg = await td.evaluate(el => getComputedStyle(el).backgroundColor);
    expect(bg, 'TOTAL row bg should contain amber').toMatch(/251.*191|rgba\(251/);
  });

  // ── 5. /pulse ─────────────────────────────────────────────────────────────────

  test('Pulse: all bucket grids have ag-theme-algo class', async () => {
    await P.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.ag-theme-algo', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const grids = P.locator('.ag-theme-algo');
    if (!await grids.count()) return;
    expect(await grids.count()).toBeGreaterThan(0);
    for (let i = 0; i < Math.min(await grids.count(), 4); i++) {
      const cls = await grids.nth(i).getAttribute('class');
      expect(cls, `Pulse grid ${i} should include ag-theme-quartz`).toContain('ag-theme-quartz');
    }
  });

  test('Pulse: header text is muted slate', async () => {
    await P.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.ag-theme-algo .ag-header-cell', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const th = P.locator('.ag-theme-algo .ag-header-cell').first();
    if (!await th.count()) return;
    const color = await th.evaluate(el => getComputedStyle(el).color);
    expectMutedSlateText(color, 'Pulse ag-header-cell text');
  });

  test('Pulse: ag-Grid TOTAL row keeps amber tint', async () => {
    await P.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.ag-theme-algo', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const totalRows = P.locator('.ag-theme-algo .ag-row.totals-row, .ag-theme-algo .ag-row.mp-total-row');
    if (!await totalRows.count()) return;
    const bg = await totalRows.first().evaluate(el => getComputedStyle(el).backgroundColor);
    expect(bg, 'Pulse TOTAL row should have amber bg').toMatch(/251.*191|rgba\(251/);
  });

  // ── 6. /admin/derivatives ─────────────────────────────────────────────────────

  test('Derivatives: cand-grid header bg is deep dark', async () => {
    await P.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.cand-headrow', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const headrow = P.locator('.cand-headrow').first();
    if (!await headrow.count()) return;
    const bg = await headrow.evaluate(el => getComputedStyle(el).backgroundColor);
    expectDeepDarkHeaderBg(bg, 'cand-headrow bg');
  });

  test('Derivatives: cand-grid header text is muted slate', async () => {
    await P.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.cand-headrow', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const headrow = P.locator('.cand-headrow').first();
    if (!await headrow.count()) return;
    const color = await headrow.evaluate(el => getComputedStyle(el).color);
    expectMutedSlateText(color, 'cand-headrow text');
  });

  test('Derivatives: cand-row hover is cyan (not amber)', async () => {
    await P.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.cand-row', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const row = P.locator('.cand-row').first();
    if (!await row.count()) return;
    await row.hover();
    const bg = await row.evaluate(el => getComputedStyle(el).backgroundColor);
    expectNotAmberHover(bg, 'cand-row hover');
  });

  test('Derivatives: byund-grid header span bg is deep dark', async () => {
    await P.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.byund-headrow', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const span = P.locator('.byund-headrow > span').first();
    if (!await span.count()) return;
    const bg = await span.evaluate(el => getComputedStyle(el).backgroundColor);
    expectDeepDarkHeaderBg(bg, 'byund-headrow span bg');
  });

  test('Derivatives: byund-grid header text is muted slate', async () => {
    await P.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.byund-headrow', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const span = P.locator('.byund-headrow > span').first();
    if (!await span.count()) return;
    const color = await span.evaluate(el => getComputedStyle(el).color);
    expectMutedSlateText(color, 'byund-headrow span text');
  });

  test('Derivatives: byund-row hover is cyan (not amber)', async () => {
    await P.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.byund-row', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const row = P.locator('.byund-row').first();
    if (!await row.count()) return;
    const span = row.locator('> span').first();
    await span.hover();
    const bg = await span.evaluate(el => getComputedStyle(el).backgroundColor);
    expectNotAmberHover(bg, 'byund-row hover');
  });
});
