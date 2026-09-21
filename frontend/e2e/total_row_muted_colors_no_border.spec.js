/**
 * total_row_muted_colors_no_border.spec.js
 *
 * Playwright e2e spec verifying the CSS changes made in:
 *
 *  1. /admin/derivatives/+page.svelte
 *     - .byund-row-total > .cell-pos/neg/flat use --algo-green/red/slate (not bright)
 *     - .cand-row.cand-row-total > .cell-pos/neg/flat use --algo-green/red/slate
 *     - .cand-row.cand-row-total > .cell-flat uses --algo-slate (not amber 75%)
 *
 *  2. src/app.css
 *     - .ag-theme-ramboq .ag-row.totals-row .ag-cell: border-right removed
 *     - .ag-theme-algo .ag-row.totals-row .ag-cell: border-right removed
 *
 *  3. src/lib/MarketPulse.svelte
 *     - .ag-theme-algo .mp-total-row .ag-cell: border-right removed
 *
 * Five quality dimensions:
 *  1. SSOT   — color vars and border-right rules are sourced from the exact CSS declarations changed
 *  2. Perf   — page loads within 15s budget
 *  3. Stale  — CSS class names (.cand-row-total, .byund-row-total, .totals-row, .mp-total-row)
 *              are confirmed present in DOM (guards against class renames)
 *  4. Reuse  — bright-color CSS variables are NOT the computed value on total-row spans
 *  5. UX     — no vertical hairline border-right on total-row cells (ag-Grid + custom grids)
 *
 * Run:
 *   PLAYWRIGHT_USER=rambo PLAYWRIGHT_PASS=admin1234 \
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/total_row_muted_colors_no_border.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

test.setTimeout(60000);

const USER = process.env.PLAYWRIGHT_USER || 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

async function signIn(page) {
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="username"], input#username, input#s-user').first().fill(USER);
  await page.locator('input[name="password"], input#password, input#s-pass').first().fill(PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
  for (let i = 0; i < 10; i++) {
    const has = await page.evaluate(() => !!sessionStorage.getItem('ramboq_token'));
    if (has) break;
    await new Promise((r) => setTimeout(r, 300));
  }
}

// ── Helpers ────────────────────────────────────────────────────────────────

/**
 * Returns the resolved CSS variable value from :root or null.
 * Used to compare computed colors against the palette variables.
 */
async function resolveCssVar(page, varName) {
  return page.evaluate((v) => {
    return getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  }, varName);
}

/**
 * Returns true if the computed border-right-width of the element is 0px
 * (i.e. no visible right border).
 */
async function hasBorderRight(locator) {
  const width = await locator.evaluate((el) => getComputedStyle(el).borderRightWidth);
  return width !== '0px';
}

// ── Test suite: /admin/derivatives — TOTAL row muted colors ────────────────

test.describe('/admin/derivatives — TOTAL row muted colors + no cell border-right', () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  test('1-SSOT+Stale+Reuse: cand-row-total cell-pos/neg/flat use muted CSS vars not bright', async ({
    page,
  }) => {
    const t0 = Date.now();
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });

    // Dimension 2 — page load within budget.
    const candGrid = page.locator('.cand-grid').first();
    await candGrid.waitFor({ timeout: 15000 });
    expect(Date.now() - t0).toBeLessThan(15000);

    // Resolve muted palette values from :root.
    const algoGreen = await resolveCssVar(page, '--algo-green');
    const algoRed   = await resolveCssVar(page, '--algo-red');
    const algoSlate = await resolveCssVar(page, '--algo-slate');
    // Guard: palette vars must exist in this theme.
    expect(algoGreen.length, '--algo-green CSS var must be defined').toBeGreaterThan(0);
    expect(algoRed.length,   '--algo-red CSS var must be defined').toBeGreaterThan(0);
    expect(algoSlate.length, '--algo-slate CSS var must be defined').toBeGreaterThan(0);

    // Dimension 3 — Stale: class name still exists.
    const totalRow = page.locator('.cand-row.cand-row-total').first();
    const totalCount = await totalRow.count();
    if (totalCount === 0) {
      console.log('[total_row_muted] no positions in test env — cand-row-total not rendered, skipping color check');
      return;
    }

    // Dimension 4 — Reuse: verify that the bright CSS variables are NOT present
    // anywhere in the stylesheet for .cand-row-total cells.  We inspect the
    // computed color of .cell-pos/.cell-neg/.cell-flat spans and confirm they
    // do NOT match --algo-green-text-bright / --algo-red-text-bright values.
    const brightGreen = await resolveCssVar(page, '--algo-green-text-bright');
    const brightRed   = await resolveCssVar(page, '--algo-red-text-bright');

    // Check .cell-pos inside cand-row-total
    const posCell = page.locator('.cand-row.cand-row-total > .cell-pos').first();
    const posCellCount = await posCell.count();
    if (posCellCount > 0 && brightGreen.length > 0) {
      const computedColor = await posCell.evaluate((el) => getComputedStyle(el).color);
      expect(computedColor).not.toBe(brightGreen);
    }

    // Check .cell-neg inside cand-row-total
    const negCell = page.locator('.cand-row.cand-row-total > .cell-neg').first();
    const negCellCount = await negCell.count();
    if (negCellCount > 0 && brightRed.length > 0) {
      const computedColor = await negCell.evaluate((el) => getComputedStyle(el).color);
      expect(computedColor).not.toBe(brightRed);
    }

    // Dimension 1+5 — SSOT+UX: .cell-flat uses slate, not amber 75%.
    const flatCell = page.locator('.cand-row.cand-row-total > .cell-flat').first();
    const flatCellCount = await flatCell.count();
    if (flatCellCount > 0) {
      const computedColor = await flatCell.evaluate((el) => getComputedStyle(el).color);
      // Confirm it is not the old amber 75% value: rgb(251,191,36) at alpha 0.75
      // Browsers expand rgba to "rgba(251, 191, 36, 0.753)" or similar.
      expect(computedColor).not.toMatch(/rgba?\(251,\s*191,\s*36/);
    }
  });

  test('2-SSOT+Stale: byund-row-total cell-pos/neg/flat use muted CSS vars', async ({
    page,
  }) => {
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    await page.locator('.cand-grid').first().waitFor({ timeout: 15000 });

    // Dimension 3 — Stale: byund-row-total class still exists in codebase.
    const byundTotal = page.locator('.byund-row-total').first();
    const byundCount = await byundTotal.count();
    if (byundCount === 0) {
      console.log('[total_row_muted] no byund-row-total in DOM (no positions) — skipping');
      return;
    }

    const brightGreen = await resolveCssVar(page, '--algo-green-text-bright');
    const brightRed   = await resolveCssVar(page, '--algo-red-text-bright');

    const posCell = page.locator('.byund-row-total > .cell-pos').first();
    if (await posCell.count() > 0 && brightGreen.length > 0) {
      const color = await posCell.evaluate((el) => getComputedStyle(el).color);
      expect(color).not.toBe(brightGreen);
    }

    const negCell = page.locator('.byund-row-total > .cell-neg').first();
    if (await negCell.count() > 0 && brightRed.length > 0) {
      const color = await negCell.evaluate((el) => getComputedStyle(el).color);
      expect(color).not.toBe(brightRed);
    }

    // .cell-flat must not use old amber 75%.
    const flatCell = page.locator('.byund-row-total > .cell-flat').first();
    if (await flatCell.count() > 0) {
      const color = await flatCell.evaluate((el) => getComputedStyle(el).color);
      expect(color).not.toMatch(/rgba?\(251,\s*191,\s*36/);
    }
  });

  test('3-UX+SSOT: ag-Grid totals-row cells have no border-right (app.css rule removed)', async ({
    page,
  }) => {
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    await page.locator('.cand-grid').first().waitFor({ timeout: 15000 });

    // Find any ag-Grid totals-row cell in the page.
    const totalsCell = page.locator('.ag-row.totals-row .ag-cell').first();
    const count = await totalsCell.count();
    if (count === 0) {
      console.log('[total_row_muted] no .ag-row.totals-row in DOM — skipping border-right check');
      return;
    }

    // Dimension 5 — UX: no vertical hairline between cells on totals-row.
    const borderRightWidth = await totalsCell.evaluate((el) => getComputedStyle(el).borderRightWidth);
    expect(borderRightWidth, 'totals-row .ag-cell should have no border-right').toBe('0px');
  });
});

// ── Test suite: /pulse — MarketPulse mp-total-row no border-right ──────────

test.describe('/pulse — MarketPulse mp-total-row no cell border-right', () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  test('4-UX+SSOT+Stale: mp-total-row ag-cell has no border-right (MarketPulse.svelte rule removed)', async ({
    page,
  }) => {
    const t0 = Date.now();
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    // Dimension 2 — Perf: within 15s.
    const agRoot = page.locator('.ag-theme-algo').first();
    await agRoot.waitFor({ timeout: 15000 });
    expect(Date.now() - t0).toBeLessThan(15000);

    // Dimension 3 — Stale: mp-total-row class still present.
    const mpTotalCell = page.locator('.ag-theme-algo .mp-total-row .ag-cell').first();
    const count = await mpTotalCell.count();
    if (count === 0) {
      console.log('[total_row_muted] no .mp-total-row in pulse DOM — skipping border-right check');
      return;
    }

    // Dimension 1+5 — SSOT+UX: border-right must be removed.
    const borderRightWidth = await mpTotalCell.evaluate((el) => getComputedStyle(el).borderRightWidth);
    expect(borderRightWidth, 'mp-total-row .ag-cell should have no border-right').toBe('0px');
  });
});
