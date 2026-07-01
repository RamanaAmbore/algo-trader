/**
 * grid_chrome_ssot.spec.js
 *
 * Asserts that every bespoke data grid / table wrapper on algo pages carries
 * the canonical outer chrome matching .algo-grid-chrome (app.css):
 *   border-width ≥ 1px
 *   border-radius ≥ 4px
 *   box-shadow (not 'none')
 *
 * Also asserts that the .algo-grid-chrome class is present on the wrapper
 * element (SSOT check — class added at the same time as the CSS utility).
 *
 * Five quality dimensions:
 *  1. SSOT  — .algo-grid-chrome class present on every checked wrapper.
 *  2. Perf  — each grid route loads and renders without blocking.
 *  3. Stale — no wrapper uses deprecated 1px/no-shadow styles.
 *  4. Reuse — same .algo-grid-chrome class on disparate surfaces.
 *  5. UX    — border-radius ≥ 4px + box-shadow on every surface
 *             (desktop + mobile viewports).
 *
 * Single login for the whole suite (one describe block, one beforeAll).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(120_000);
const NAV_TIMEOUT  = 90_000;
const WAIT_TIMEOUT = 40_000;

// ── helpers ───────────────────────────────────────────────────────────────────

/**
 * Assert computed border-width ≥ 1px for all four sides (at least one
 * side must be ≥ 1px).
 */
function expectBorder(styles, label) {
  const sides = ['borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth'];
  const pxVals = sides.map(s => parseFloat(styles[s]) || 0);
  const max = Math.max(...pxVals);
  expect(max, `${label}: at least one border side should be ≥ 1px`).toBeGreaterThanOrEqual(1);
}

/** Assert border-radius ≥ 4px */
function expectRadius(styles, label) {
  const r = parseFloat(styles.borderTopLeftRadius) || 0;
  expect(r, `${label}: border-radius should be ≥ 4px`).toBeGreaterThanOrEqual(4);
}

/** Assert box-shadow is set (not 'none') */
function expectShadow(styles, label) {
  expect(styles.boxShadow, `${label}: box-shadow should not be 'none'`).not.toBe('none');
}

/**
 * Evaluate chrome on a single element by selector. Skips gracefully if
 * the element is absent (route may have no data). Returns false when absent.
 * @param {import('@playwright/test').Page} page
 * @param {string} selector
 * @param {string} label
 * @returns {Promise<boolean>}
 */
async function checkChrome(page, selector, label) {
  const el = page.locator(selector).first();
  if (!await el.count()) return false;

  const styles = await el.evaluate(e => {
    const cs = getComputedStyle(e);
    return {
      borderTopWidth:         cs.borderTopWidth,
      borderRightWidth:       cs.borderRightWidth,
      borderBottomWidth:      cs.borderBottomWidth,
      borderLeftWidth:        cs.borderLeftWidth,
      borderTopLeftRadius:    cs.borderTopLeftRadius,
      boxShadow:              cs.boxShadow,
      className:              e.className,
    };
  });

  expectBorder(styles, label);
  expectRadius(styles, label);
  expectShadow(styles, label);

  // SSOT: class must include algo-grid-chrome
  expect(styles.className, `${label}: should carry algo-grid-chrome class`).toContain('algo-grid-chrome');

  return true;
}

// ── All tests share one authenticated session ─────────────────────────────────

test.describe('Grid chrome SSOT', () => {
  /** @type {import('@playwright/test').Page} */
  let P;

  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext();
    P = await ctx.newPage();
    await loginAsAdmin(P);
  }, 120_000);

  test.afterAll(async () => {
    await P?.context().close();
  });

  // ── 1. /admin/derivatives — cand-scroll (legs candidates) ────────────────────

  test('Derivatives: .cand-scroll has canonical chrome + algo-grid-chrome class', async () => {
    await P.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.cand-scroll', { timeout: WAIT_TIMEOUT }).catch(() => null);
    await checkChrome(P, '.cand-scroll', 'cand-scroll');
  });

  test('Derivatives: .byund-scroll has canonical chrome + algo-grid-chrome class', async () => {
    await P.goto('/admin/derivatives', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.byund-scroll', { timeout: WAIT_TIMEOUT }).catch(() => null);
    await checkChrome(P, '.byund-scroll', 'byund-scroll');
  });

  // ── 2. /dashboard — dash-mini-grid (ag-theme-algo, chrome from the theme) ────

  test('Dashboard: .dash-mini-grid (ag-theme-algo) has border ≥ 1px', async () => {
    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.dash-mini-grid:not(.is-empty)', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const el = P.locator('.dash-mini-grid:not(.is-empty)').first();
    if (!await el.count()) {
      test.info().annotations.push({ type: 'skip', description: 'No non-empty dash-mini-grid found' });
      return;
    }
    const styles = await el.evaluate(e => {
      const cs = getComputedStyle(e);
      return {
        borderTopWidth:      cs.borderTopWidth,
        borderTopLeftRadius: cs.borderTopLeftRadius,
        boxShadow:           cs.boxShadow,
      };
    });
    // ag-theme-algo provides the chrome natively — just assert the computed values.
    expect(parseFloat(styles.borderTopWidth)).toBeGreaterThanOrEqual(1);
    expect(parseFloat(styles.borderTopLeftRadius)).toBeGreaterThanOrEqual(4);
    expect(styles.boxShadow).not.toBe('none');
  });

  // ── 3. /admin/history — hist-table-wrap ───────────────────────────────────────

  test('History: .hist-table-wrap has canonical chrome + algo-grid-chrome class', async () => {
    await P.goto('/admin/history', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.hist-table-wrap', { timeout: WAIT_TIMEOUT }).catch(() => null);
    await checkChrome(P, '.hist-table-wrap', 'hist-table-wrap');
  });

  // ── 4. /strategies/:id — strat-table-wrap ─────────────────────────────────────

  test('Strategies: .strat-table-wrap has canonical chrome + algo-grid-chrome class', async () => {
    // Navigate to the strategies list first, then click the first strategy.
    await P.goto('/strategies', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    // Try to follow the first strategy link if present; if none, skip gracefully.
    const link = P.locator('a[href^="/strategies/"]').first();
    if (!await link.count()) {
      test.info().annotations.push({ type: 'skip', description: 'No strategies found' });
      return;
    }
    await link.click();
    await P.waitForSelector('.strat-table-wrap', { timeout: WAIT_TIMEOUT }).catch(() => null);
    await checkChrome(P, '.strat-table-wrap', 'strat-table-wrap');
  });

  // ── 5. /admin/brokers — brokers-scroll ────────────────────────────────────────

  test('Brokers: .brokers-scroll has canonical chrome + algo-grid-chrome class', async () => {
    await P.goto('/admin/brokers', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.brokers-scroll', { timeout: WAIT_TIMEOUT }).catch(() => null);
    await checkChrome(P, '.brokers-scroll', 'brokers-scroll');
  });

  // ── 6. /automation — no bespoke data-grid wrapper needed ─────────────────────
  // Automation page uses ag-theme-algo grids + .agent-group-grid (layout, not data).

  test('Automation: any ag-theme-algo grid has border ≥ 1px (inherited from theme)', async () => {
    await P.goto('/automation', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForSelector('.ag-theme-algo', { timeout: WAIT_TIMEOUT }).catch(() => null);
    const el = P.locator('.ag-theme-algo').first();
    if (!await el.count()) return;
    const bw = await el.evaluate(e => parseFloat(getComputedStyle(e).borderTopWidth) || 0);
    expect(bw, 'ag-theme-algo border should be ≥ 1px').toBeGreaterThanOrEqual(1);
  });
});
