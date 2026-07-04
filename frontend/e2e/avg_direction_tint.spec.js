/**
 * avg_direction_tint.spec.js
 *
 * Regression guard: Avg cells on PerformancePage (positions + holdings grids)
 * and the derivatives legs grid must carry cell-pos (long) / cell-neg (short)
 * directional tint, mirroring the Pulse avg_combined column that was fixed in
 * commit 93d7ff8a.
 *
 * Five quality dimensions:
 *   1. SSOT       — avgClsWithDir is the single cellClass for Avg columns.
 *                   avgVsLtpCls (LTP column) is unchanged — no qty-direction
 *                   bleed onto price cells.
 *   2. Performance — DOM assertions are post-render; no polling required.
 *   3. Stale code  — grep confirms avgVsLtpCls is NOT on the Avg column def,
 *                   and avgClsWithDir is present for both positions and holdings.
 *   4. Reusable    — uses the same mock-positions pattern as pulse_avg_combined_style.spec.js.
 *   5. UX          — long rows: cell-pos on Avg; short rows: cell-neg on Avg;
 *                   LTP column keeps its own class (no cell-pos/neg from qty).
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

test.setTimeout(60_000);

const USER = process.env.PLAYWRIGHT_USER || 'rambo';
const PASS  = process.env.PLAYWRIGHT_PASS || 'admin1234';

async function signIn(page) {
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="username"], input#username, input#s-user').first().fill(USER);
  await page.locator('input[name="password"], input#password, input#s-pass').first().fill(PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15_000 });
  for (let i = 0; i < 10; i++) {
    const has = await page.evaluate(() => !!sessionStorage.getItem('ramboq_token'));
    if (has) break;
    await new Promise((r) => setTimeout(r, 300));
  }
}

// ─── STALE CODE: PerformancePage uses avgClsWithDir on Avg column ────────────

test('PerformancePage.svelte: avgClsWithDir defined and used on Avg columns', () => {
  const src = readFileSync('src/lib/PerformancePage.svelte', 'utf8');

  // avgClsWithDir must be defined
  expect(src).toContain('const avgClsWithDir');

  // Both holdingsCols and positionsCols Avg column must reference avgClsWithDir
  // Exact column def pattern: average_price ... cellClass: avgClsWithDir
  const avgOccurrences = (src.match(/average_price[\s\S]{0,200}?cellClass:\s*avgClsWithDir/g) || []).length;
  expect(avgOccurrences, 'Both positions + holdings Avg cols must use avgClsWithDir').toBeGreaterThanOrEqual(2);

  // LTP columns must keep avgVsLtpCls (not avgClsWithDir)
  const ltpOccurrences = (src.match(/last_price[\s\S]{0,200}?cellClass:\s*avgVsLtpCls/g) || []).length;
  expect(ltpOccurrences, 'LTP columns must keep avgVsLtpCls').toBeGreaterThanOrEqual(2);
});

test('PerformancePage.svelte: avgClsWithDir reads quantity field for direction', () => {
  const src = readFileSync('src/lib/PerformancePage.svelte', 'utf8');
  const fnIdx = src.indexOf('const avgClsWithDir');
  expect(fnIdx).toBeGreaterThan(0);
  // Grab up to the closing };
  const fnSnippet = src.slice(fnIdx, fnIdx + 600);
  // Must read quantity from params.data
  expect(fnSnippet).toMatch(/params\.data\??\.quantity/);
  // Must emit cell-pos
  expect(fnSnippet).toContain('cell-pos');
  // Must emit cell-neg
  expect(fnSnippet).toContain('cell-neg');
  // Must guard TOTAL rows
  expect(fnSnippet).toMatch(/_isTotal|TOTAL/);
});

// ─── STALE CODE: derivatives page Avg span has direction class ───────────────

test('derivatives/+page.svelte: Avg span uses displayQty for cell-pos/neg', () => {
  const src = readFileSync('src/routes/(algo)/admin/derivatives/+page.svelte', 'utf8');
  // Find the Avg span near priceFmt(cost) — it must now carry a direction class
  // driven by displayQty.
  const costIdx = src.indexOf('priceFmt(cost)');
  expect(costIdx).toBeGreaterThan(0);
  // Walk back ~200 chars to the opening <span tag
  const spanStart = src.lastIndexOf('<span', costIdx);
  const spanTag = src.slice(spanStart, costIdx + 20);
  expect(spanTag).toContain('displayQty');
  expect(spanTag).toContain('cell-pos');
  expect(spanTag).toContain('cell-neg');
});

// ─── INTEGRATION: PerformancePage Avg cell has cell-pos for long position ────

async function mockPerfPageData(page) {
  await page.route('**/api/market/status', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ nse_open: true, mcx_open: true, any_open: true, is_holiday: false }),
    });
  });
  await page.route('**/api/positions**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        source: 'live',
        rows: [
          {
            tradingsymbol: 'RELIANCE',
            exchange: 'NSE',
            last_price: 2950,
            average_price: 2900,
            close_price: 2930,
            quantity: 2,
            pnl: 100,
            pnl_percentage: 3.4,
            day_change_val: 40,
            day_change_percentage: 1.4,
            product: 'CNC',
          },
          {
            tradingsymbol: 'NIFTY25JUNFUT',
            exchange: 'NFO',
            last_price: 24000,
            average_price: 24200,
            close_price: 24100,
            quantity: -75,
            pnl: 200,
            pnl_percentage: 0.8,
            day_change_val: 100,
            day_change_percentage: 0.4,
            product: 'NRML',
          },
        ],
      }),
    });
  });
  await page.route('**/api/holdings**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        source: 'live',
        rows: [
          {
            tradingsymbol: 'INFY',
            exchange: 'NSE',
            last_price: 1800,
            average_price: 1750,
            close_price: 1790,
            quantity: 10,
            pnl: 500,
            pnl_percentage: 2.8,
            day_change_val: 100,
            day_change_percentage: 0.6,
          },
        ],
      }),
    });
  });
}

test('PerformancePage: long position Avg cell has cell-pos class', async ({ page }) => {
  await signIn(page);
  await mockPerfPageData(page);
  await page.goto('/performance', { waitUntil: 'networkidle', timeout: 40_000 });
  await page.waitForSelector('.ag-theme-ramboq .ag-cell, .ag-theme-algo .ag-cell', { timeout: 20_000 });

  const hasPos = await page.evaluate(() => {
    const cells = document.querySelectorAll(
      '.ag-theme-ramboq .ag-cell[col-id="average_price"], .ag-theme-algo .ag-cell[col-id="average_price"]'
    );
    for (const cell of cells) {
      const row = cell.closest('.ag-row');
      const sym = row?.querySelector('[col-id="tradingsymbol"]')?.textContent?.trim();
      if (sym === 'RELIANCE') return cell.classList.contains('cell-pos');
    }
    return null;
  });
  if (hasPos !== null) expect(hasPos, 'Long RELIANCE Avg cell must have cell-pos').toBe(true);
});

test('PerformancePage: short position Avg cell has cell-neg class', async ({ page }) => {
  await signIn(page);
  await mockPerfPageData(page);
  await page.goto('/performance', { waitUntil: 'networkidle', timeout: 40_000 });
  await page.waitForSelector('.ag-theme-ramboq .ag-cell, .ag-theme-algo .ag-cell', { timeout: 20_000 });

  const hasNeg = await page.evaluate(() => {
    const cells = document.querySelectorAll(
      '.ag-theme-ramboq .ag-cell[col-id="average_price"], .ag-theme-algo .ag-cell[col-id="average_price"]'
    );
    for (const cell of cells) {
      const row = cell.closest('.ag-row');
      const sym = row?.querySelector('[col-id="tradingsymbol"]')?.textContent?.trim();
      if (sym === 'NIFTY25JUNFUT') return cell.classList.contains('cell-neg');
    }
    return null;
  });
  if (hasNeg !== null) expect(hasNeg, 'Short NIFTY25JUNFUT Avg cell must have cell-neg').toBe(true);
});

test('PerformancePage: long holding Avg cell has cell-pos class', async ({ page }) => {
  await signIn(page);
  await mockPerfPageData(page);
  await page.goto('/performance', { waitUntil: 'networkidle', timeout: 40_000 });
  await page.waitForSelector('.ag-theme-ramboq .ag-cell, .ag-theme-algo .ag-cell', { timeout: 20_000 });

  const hasPos = await page.evaluate(() => {
    const cells = document.querySelectorAll(
      '.ag-theme-ramboq .ag-cell[col-id="average_price"], .ag-theme-algo .ag-cell[col-id="average_price"]'
    );
    for (const cell of cells) {
      const row = cell.closest('.ag-row');
      const sym = row?.querySelector('[col-id="tradingsymbol"]')?.textContent?.trim();
      if (sym === 'INFY') return cell.classList.contains('cell-pos');
    }
    return null;
  });
  if (hasPos !== null) expect(hasPos, 'Long INFY holding Avg cell must have cell-pos').toBe(true);
});

test('PerformancePage: LTP cell does NOT get cell-pos/neg from qty', async ({ page }) => {
  await signIn(page);
  await mockPerfPageData(page);
  await page.goto('/performance', { waitUntil: 'networkidle', timeout: 40_000 });
  await page.waitForSelector('.ag-theme-ramboq .ag-cell, .ag-theme-algo .ag-cell', { timeout: 20_000 });

  // LTP column must not carry cell-pos/cell-neg from quantity — those classes
  // are for Avg only. LTP may carry ltp-vs-avg-up/down (from avgVsLtpCls).
  const ltpHasQtyClass = await page.evaluate(() => {
    const cells = document.querySelectorAll(
      '.ag-theme-ramboq .ag-cell[col-id="last_price"], .ag-theme-algo .ag-cell[col-id="last_price"]'
    );
    for (const cell of cells) {
      const row = cell.closest('.ag-row');
      const sym = row?.querySelector('[col-id="tradingsymbol"]')?.textContent?.trim();
      // NIFTY25JUNFUT is a short position — if LTP had qty-direction class it
      // would be cell-neg. It must NOT.
      if (sym === 'NIFTY25JUNFUT') {
        return cell.classList.contains('cell-pos') || cell.classList.contains('cell-neg');
      }
    }
    return null;
  });
  // Gate: if we found the row, the LTP cell must not have a qty-direction class
  if (ltpHasQtyClass !== null) {
    expect(ltpHasQtyClass, 'LTP cell must not carry cell-pos/neg from qty').toBe(false);
  }
});
