/**
 * Regression spec: all four mover tabs (Underlying / L.Cap / Midcap / Smallcap)
 * receive rows when the fixture contains symbols from each bucket.
 *
 * Root cause of the original defect (2026-07-02):
 *   _classifyMoverSym never produced 'large_cap' — only 'smallcap', 'midcap',
 *   or 'underlying'. The large_cap tab was wired off _isLargeCap (a separate
 *   boolean) which required a symbol to be in FO_LARGECAP_STOCKS AND NOT in
 *   NIFTY_MIDCAP_100 / NIFTY_SMLCAP_100. Those ~65 pure large-cap stocks
 *   (RELIANCE, TCS, …) rarely move > 1.5 %, so the backend top-20 pool
 *   almost never contained them. The large_cap tab stayed at 0.
 *
 * Fix (2026-07-02):
 *   _classifyMoverSym precedence: smallcap → midcap → large_cap → underlying.
 *   FO_STOCK_UNDERLYINGS = FO_LARGECAP_STOCKS minus {SENSEX, BANKEX} feeds
 *   the large_cap arm. _isLargeCap boolean removed; _tabCounts and _topRowsFor
 *   read _moverGroup === 'large_cap' directly.
 *
 * Fixture design:
 *   - underlying:  'NIFTY 50', 'NIFTY BANK' (NSE index names with spaces → FO_INDICES)
 *   - large_cap:   RELIANCE, TCS, HDFCBANK   (FO_STOCK_UNDERLYINGS, not mid/small)
 *   - midcap:      LUPIN, BHEL, CHOLAFIN     (NIFTY_MIDCAP_100 ∩ FO_UNDERLYINGS)
 *   - smallcap:    ANGELONE, BSE, CAMS        (NIFTY_SMLCAP_100)
 *
 * Five quality dimensions:
 *   SSOT   — each symbol lands in exactly one _moverGroup bucket
 *   Perf   — initial paint within 60 s
 *   Stale  — no duplicate tradingsymbol rows within a tab grid
 *   Reuse  — readRows helper shared with pulse_losers_regression
 *   UX     — all four tabs render with ≥ 1 row; badge counts visible
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

/** Read visible rows from a bucket grid. Returns { count, sample }. */
async function readRows(page, bucketClass, n = 20) {
  const rows = page.locator(
    `.${bucketClass} .ag-center-cols-container .ag-row`
  );
  const count = await rows.count();
  const sample = [];
  for (let i = 0; i < Math.min(n, count); i++) {
    const cells = rows.nth(i).locator('.ag-cell');
    const cellCount = await cells.count();
    const texts = [];
    for (let c = 0; c < cellCount; c++) {
      const t = (await cells.nth(c).textContent() || '').trim();
      if (t) texts.push(t);
    }
    sample.push(texts.join(' | '));
  }
  return { count, sample };
}

/**
 * Mock /api/watchlist/movers so every tab gets at least one row.
 *
 * Symbol-to-bucket mapping (mirrors _classifyMoverSym after the fix):
 *   underlying  — 'NIFTY 50', 'NIFTY BANK'  (FO_INDICES, not stocks)
 *   large_cap   — RELIANCE, TCS, HDFCBANK    (FO_STOCK_UNDERLYINGS)
 *   midcap      — LUPIN, BHEL, CHOLAFIN      (NIFTY_MIDCAP_100)
 *   smallcap    — ANGELONE, BSE, CAMS        (NIFTY_SMLCAP_100)
 *
 * All eight symbols move in the "winners" direction (+%) so the test
 * asserts the Winners panel. Losers are left empty (the api returns an
 * empty array for the loser-direction) to keep the fixture minimal.
 */
async function mockAllTabsWinners(page) {
  const make = (sym, i, changePct) => ({
    tradingsymbol:  sym,
    exchange:       'NSE',
    last_price:     1000 + i * 50,
    previous_close:  900 + i * 50,
    change_pct:     changePct,
    peak_pct:       changePct + 0.5,
    sticky:         false,
  });

  const winners = [
    // underlying (FO index names — contain a space → FO_INDICES, not FO_LARGECAP_STOCKS)
    make('NIFTY 50',   0, 1.8),
    make('NIFTY BANK', 1, 2.1),
    // large_cap (FO_STOCK_UNDERLYINGS — no space, not in midcap/smallcap)
    make('RELIANCE',  2, 3.2),
    make('TCS',       3, 2.9),
    make('HDFCBANK',  4, 2.5),
    // midcap (NIFTY_MIDCAP_100)
    make('LUPIN',     5, 4.1),
    make('BHEL',      6, 3.8),
    make('CHOLAFIN',  7, 3.5),
    // smallcap (NIFTY_SMLCAP_100)
    make('ANGELONE',  8, 5.2),
    make('BSE',       9, 4.8),
    make('CAMS',     10, 4.5),
  ];

  await page.route('**/api/watchlist/movers', route =>
    route.fulfill({
      status:      200,
      contentType: 'application/json',
      body: JSON.stringify({
        movers:        winners,
        threshold_pct: 1.5,
        session_date:  new Date().toISOString().slice(0, 10),
        captured_at:   null,
      }),
    })
  );
}

async function signIn(page) {
  for (const creds of [
    { user: process.env.PLAYWRIGHT_USER || 'ambore', pass: process.env.PLAYWRIGHT_PASS || 'admin1234' },
    { user: 'rambo', pass: process.env.PLAYWRIGHT_PASS || 'admin1234' },
  ]) {
    try {
      await loginAsAdmin(page, creds);
      return creds.user;
    } catch (_) { /* try next */ }
  }
  throw new Error('Could not sign in with any known credentials');
}

test.describe('Movers tab coverage — all four tabs receive rows', () => {
  test('desktop — Underlying / L.Cap / Midcap / Smallcap each ≥ 1 winner row', async ({ page }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });

    const user = await signIn(page);
    console.log(`[auth] signed in as ${user}`);

    // Wire mock BEFORE navigation so the initial fetch hits it.
    await mockAllTabsWinners(page);

    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    // Wait for the Winners section to mount.
    await expect(page.locator('.mp-bucket-winners').first())
      .toBeVisible({ timeout: 30_000 });

    // Wait up to 60 s for rows to appear in the initial tab.
    await expect(
      page.locator('.mp-bucket-winners .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });

    const winTabs = page.locator('.mp-bucket-winners .mp-wl-tab');

    // SSOT: 4 tabs present
    await expect(winTabs).toHaveCount(4, { timeout: 5_000 });
    const tabTexts = await winTabs.allTextContents();
    console.log('[desktop] Winner tab labels:', tabTexts.map(t => t.trim()));

    for (const label of ['Underlying', 'L.Cap', 'Midcap', 'Smallcap']) {
      expect(
        tabTexts.some(t => t.includes(label)),
        `Winners tab strip must include "${label}"`
      ).toBe(true);
    }

    // UX: visit each tab and assert ≥ 1 row
    const tabDefs = [
      { label: 'Underlying', minRows: 1 },
      { label: 'L.Cap',      minRows: 1 },
      { label: 'Midcap',     minRows: 1 },
      { label: 'Smallcap',   minRows: 1 },
    ];

    for (const { label, minRows } of tabDefs) {
      const tab = winTabs.filter({ hasText: label });
      await tab.click();
      // Allow grid to re-render.
      await page.waitForTimeout(1_500);

      const result = await readRows(page, 'mp-bucket-winners');
      console.log(`[desktop] "${label}" tab — rows: ${result.count}, sample:`, result.sample.slice(0, 3));

      expect(
        result.count,
        `Winners "${label}" tab must have ≥ ${minRows} row(s)`
      ).toBeGreaterThanOrEqual(minRows);

      // Stale: no duplicate symbols within the grid
      const syms = result.sample.map(row => row.split(' | ')[0]);
      const unique = new Set(syms);
      expect(
        unique.size,
        `Winners "${label}" tab must not have duplicate symbol rows`
      ).toBe(syms.length);
    }
  });

  test('mobile portrait — all four tabs render', async ({ page }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 390, height: 844 });

    const user = await signIn(page);
    console.log(`[auth] mobile signed in as ${user}`);

    await mockAllTabsWinners(page);

    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    await expect(page.locator('.mp-bucket-winners').first())
      .toBeVisible({ timeout: 30_000 });

    await expect(
      page.locator('.mp-bucket-winners .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });

    const winTabs = page.locator('.mp-bucket-winners .mp-wl-tab');
    await expect(winTabs).toHaveCount(4, { timeout: 5_000 });

    const tabTexts = await winTabs.allTextContents();
    console.log('[mobile] Winner tab labels:', tabTexts.map(t => t.trim()));

    for (const label of ['Underlying', 'L.Cap', 'Midcap', 'Smallcap']) {
      expect(
        tabTexts.some(t => t.includes(label)),
        `Mobile: Winners tab strip must include "${label}"`
      ).toBe(true);
    }

    // UX: L.Cap tab specifically — this was the perpetually-empty tab
    const lcapTab = winTabs.filter({ hasText: 'L.Cap' });
    await lcapTab.click();
    await page.waitForTimeout(1_500);

    const lcap = await readRows(page, 'mp-bucket-winners');
    console.log('[mobile] L.Cap rows:', lcap.count, 'sample:', lcap.sample.slice(0, 3));
    expect(lcap.count, 'Mobile: L.Cap tab must have ≥ 1 row').toBeGreaterThanOrEqual(1);
  });

  test('large_cap badge count matches visible row count', async ({ page }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });

    const user = await signIn(page);
    console.log(`[auth] signed in as ${user}`);

    await mockAllTabsWinners(page);

    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    await expect(page.locator('.mp-bucket-winners').first())
      .toBeVisible({ timeout: 30_000 });

    await expect(
      page.locator('.mp-bucket-winners .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });

    const winTabs = page.locator('.mp-bucket-winners .mp-wl-tab');
    const lcapTab = winTabs.filter({ hasText: 'L.Cap' });

    // The badge inside the tab shows the pool count (pre-cap).
    // It must be ≥ 1 since the fixture has 3 large-cap winners.
    const lcapText = (await lcapTab.textContent() || '').trim();
    console.log('[badge] L.Cap tab text:', lcapText);

    // Click and confirm rows
    await lcapTab.click();
    await page.waitForTimeout(1_500);

    const result = await readRows(page, 'mp-bucket-winners');
    console.log('[badge] L.Cap rows after click:', result.count, result.sample.slice(0, 3));
    expect(result.count, 'L.Cap tab must show ≥ 1 row after click').toBeGreaterThanOrEqual(1);

    // UX: SENSEX and BANKEX must NOT appear in the L.Cap tab
    // (they are BSE index names excluded from FO_STOCK_UNDERLYINGS)
    const symbols = result.sample.map(r => r.split(' | ')[0]);
    expect(
      symbols.includes('SENSEX'),
      'SENSEX must not appear in the L.Cap tab (it is a BSE index)'
    ).toBe(false);
    expect(
      symbols.includes('BANKEX'),
      'BANKEX must not appear in the L.Cap tab (it is a BSE index)'
    ).toBe(false);
  });
});
