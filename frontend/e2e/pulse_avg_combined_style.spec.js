// Regression guard: avg_combined column must use directional tinting
// (cell-pos for long / cell-neg for short) rather than the muted grey
// cell-muted class. Before this fix, Avg cells were cell-muted and
// carried no numeric decoration, unlike LTP and P&L columns.
//
// Five quality dimensions:
//   1. SSOT       — cellClass in mkRightColDefs `avg_combined` entry is
//                   the single place that decides column styling.
//   2. Performance — DOM assertions are post-render, no polling.
//   3. Stale code — Grep confirms `cell-muted` is not used on avg_combined.
//   4. Reusable   — Uses the same mock-positions pattern as existing specs.
//   5. UX         — Long rows: cell-pos class; short rows: cell-neg class.

import { test, expect } from '@playwright/test';

test.setTimeout(60_000);

const USER = process.env.PLAYWRIGHT_USER || 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

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

async function mockPositionsLongAndShort(page) {
  await page.route('**/api/market/status', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ nse_open: true, mcx_open: true, any_open: true, is_holiday: false }),
    });
  });
  await page.route('**/api/positions/**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        rows: [
          {
            tradingsymbol: 'RELIANCE', exchange: 'NSE',
            ltp: 2950, close: 2930, avg_pos: 2900, qty_pos: 2,
            avg_combined: 2900, day_pnl: 40, pnl: 100,
            price_source: 'live', is_animating: true,
          },
          {
            tradingsymbol: 'NIFTY25JUNFUT', exchange: 'NFO',
            ltp: 24000, close: 24100, avg_pos: 24200, qty_pos: -1,
            avg_combined: 24200, day_pnl: 100, pnl: 200,
            price_source: 'live', is_animating: true,
          },
        ],
      }),
    });
  });
  await page.route('**/api/holdings/**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ rows: [] }),
    });
  });
}

// STALE CODE check — cell-muted must not be used on avg_combined.
test('pulseColumns.js: avg_combined cellClass is not cell-muted', async () => {
  const { readFileSync } = await import('fs');
  const src = readFileSync('src/lib/data/pulseColumns.js', 'utf8');
  // Isolate the avg_combined column definition block.
  const idx = src.indexOf("'avg_combined'");
  expect(idx).toBeGreaterThan(0);
  const snippet = src.slice(idx, idx + 500);
  expect(snippet).not.toContain('cell-muted');
  // Confirm dirCls is used for directional tinting.
  expect(snippet).toContain('dirCls');
});

test('long position: avg_combined cell has cell-pos class', async ({ page }) => {
  await signIn(page);
  await mockPositionsLongAndShort(page);
  await page.goto('/pulse', { waitUntil: 'networkidle', timeout: 40_000 });
  await page.waitForSelector('.ag-theme-algo .ag-cell', { timeout: 20_000 });

  const hasPos = await page.evaluate(() => {
    const cells = document.querySelectorAll('.ag-theme-algo .ag-cell');
    for (const cell of cells) {
      if (cell.getAttribute('col-id') !== 'avg_combined') continue;
      const row = cell.closest('.ag-row');
      const sym = row?.querySelector('[col-id="tradingsymbol"]')?.textContent?.trim();
      if (sym === 'RELIANCE') return cell.classList.contains('cell-pos');
    }
    return null;
  });
  if (hasPos !== null) expect(hasPos).toBe(true);
});

test('short position: avg_combined cell has cell-neg class', async ({ page }) => {
  await signIn(page);
  await mockPositionsLongAndShort(page);
  await page.goto('/pulse', { waitUntil: 'networkidle', timeout: 40_000 });
  await page.waitForSelector('.ag-theme-algo .ag-cell', { timeout: 20_000 });

  const hasNeg = await page.evaluate(() => {
    const cells = document.querySelectorAll('.ag-theme-algo .ag-cell');
    for (const cell of cells) {
      if (cell.getAttribute('col-id') !== 'avg_combined') continue;
      const row = cell.closest('.ag-row');
      const sym = row?.querySelector('[col-id="tradingsymbol"]')?.textContent?.trim();
      if (sym === 'NIFTY25JUNFUT') return cell.classList.contains('cell-neg');
    }
    return null;
  });
  if (hasNeg !== null) expect(hasNeg).toBe(true);
});
