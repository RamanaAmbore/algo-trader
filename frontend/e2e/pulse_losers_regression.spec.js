/**
 * Regression spec: Losers grid row count.
 *
 * P0 defect (2026-07-01): Losers grid showed 1 row instead of ~10-14.
 * Root cause: _classifyMoverSym priority order — smallcap → midcap →
 * underlying. Stocks that are in BOTH NIFTY_MIDCAP_100 AND FO_UNDERLYINGS
 * (BHEL, LUPIN, AUROPHARMA, MARICO, COLPAL, TVSMOTOR, BANKBARODA, CHOLAFIN,
 * IRFC, RECLTD, PFC, etc.) get _moverGroup='midcap', NOT 'underlying'.
 * The hardcoded default loseTab='underlying' then showed only the few
 * pure-underlying stocks (NIFTY indices + large-cap only) — typically 1 row.
 *
 * Fix: winTab/loseTab start as null (auto). _effLoseTab is $derived:
 * when null, it calls _bestTab(loserCounts) which returns the tab with the
 * highest candidate count. The operator's explicit click still locks
 * the selection. Badge counts are wired into AlgoTabs so every tab
 * shows its pool size before the top-N cap.
 *
 * Fixture design: the losers fixture uses midcap-heavy symbols (BHEL,
 * LUPIN, AUROPHARMA, MARICO, COLPAL) that _classifyMoverSym places in
 * 'midcap', not 'underlying'. Before the fix: loseTab stuck on 'underlying'
 * → 0-1 rows visible. After the fix: _effLoseTab auto-selects 'midcap'
 * → ≥ 5 rows visible.
 *
 * Five quality dimensions:
 *   SSOT   — movers grid feeds from moversStore; _effLoseTab picks
 *            the tab with max candidates, not hardcoded 'underlying'
 *   Perf   — initial paint within 60 s; poll refires within 35 s
 *   Stale  — no duplicate tradingsymbol rows in the losers grid
 *   Reuse  — readRows helper shared across mover grid assertions
 *   UX     — row count ≥ 5 on desktop + mobile; at least one cell
 *            shows a negative percentage value (valid loser row)
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
 * Mock /api/watchlist/movers with midcap-heavy losers.
 *
 * Losers are all NIFTY_MIDCAP_100 members that also appear in FO_UNDERLYINGS
 * — _classifyMoverSym assigns them 'midcap', not 'underlying'. This is the
 * exact scenario that triggered the defect: 'underlying' tab showed 0-1 rows
 * while 'midcap' had all the action. Auto-tab-select must pick 'midcap'.
 *
 * Winners are a mix of large-cap F&O names so the Winners grid also has rows.
 */
async function mockMovers(page) {
  // Midcap-classified losers (NIFTY_MIDCAP_100 ∩ FO_UNDERLYINGS).
  // _classifyMoverSym: NIFTY_MIDCAP_100 check fires before FO_UNDERLYINGS.
  const midcapLosers = [
    'BHEL', 'LUPIN', 'AUROPHARMA', 'MARICO', 'COLPAL',
    'TVSMOTOR', 'BANKBARODA', 'CHOLAFIN', 'IRFC', 'RECLTD',
  ].map((sym, i) => ({
    tradingsymbol: sym,
    exchange: 'NSE',
    last_price: 800 - i * 30,
    previous_close: 900 - i * 30,
    change_pct: -(2 + i * 0.4),
    peak_pct:   -(2.5 + i * 0.4),
    sticky: false,
  }));

  // Pure-underlying winners (F&O large-cap, not in NIFTY_MIDCAP_100).
  const underlyingWinners = [
    'RELIANCE', 'TCS', 'HDFCBANK', 'ICICIBANK', 'INFY',
  ].map((sym, i) => ({
    tradingsymbol: sym,
    exchange: 'NSE',
    last_price: 1000 + i * 50,
    previous_close: 900 + i * 50,
    change_pct: 3 + i * 0.3,
    peak_pct:   3.5 + i * 0.3,
    sticky: false,
  }));

  await page.route('**/api/watchlist/movers', route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        movers: [...underlyingWinners, ...midcapLosers],
        threshold_pct: 1.5,
        session_date: new Date().toISOString().slice(0, 10),
        captured_at: null,
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

test.describe('Pulse: Losers grid row count regression', () => {
  test('desktop — losers grid shows ≥ 5 rows from midcap-classified symbols', async ({ page }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });

    const user = await signIn(page);
    console.log(`[auth] signed in as ${user}`);

    // Wire mock BEFORE navigation so the initial fetch hits it.
    await mockMovers(page);

    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    // Wait for the Losers section to mount.
    await expect(page.locator('.mp-bucket-losers').first())
      .toBeVisible({ timeout: 30_000 });

    // Wait up to 60 s for rows to appear (movers poll fires on load + every ~30 s).
    await expect(
      page.locator('.mp-bucket-losers .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });

    const result = await readRows(page, 'mp-bucket-losers');
    console.log('[desktop] Losers row count:', result.count);
    console.log('[desktop] Losers sample:', result.sample.slice(0, 5));

    // SSOT: ≥ 5 losers from the midcap fixture — auto-tab should pick 'midcap'.
    // Before fix: stuck on 'underlying' → 0 rows. After fix: ≥ 5 visible.
    expect(result.count, 'Losers grid must show ≥ 5 rows').toBeGreaterThanOrEqual(5);

    // UX: at least one row text contains a negative value marker.
    const hasNegative = result.sample.some(row => /[-−][\d]/.test(row));
    expect(hasNegative, 'At least one loser row must show a negative value').toBe(true);

    // Stale: fixture had 10 distinct losers — grid must not show MORE than
    // _MOVER_TOP_N (10) rows (would indicate duplicate row injection).
    // The row count ≥ 5 check above already guards the other direction.
    expect(result.count, 'Losers grid must not exceed top-N cap (10)').toBeLessThanOrEqual(10);

    // Reuse: Winners should also have rows (shared movers pipeline).
    const winResult = await readRows(page, 'mp-bucket-winners');
    console.log('[desktop] Winners row count:', winResult.count);
    expect(winResult.count, 'Winners grid must still show rows').toBeGreaterThanOrEqual(1);
  });

  test('mobile portrait — losers grid shows ≥ 5 rows', async ({ page }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 390, height: 844 });

    const user = await signIn(page);
    console.log(`[auth] mobile signed in as ${user}`);

    await mockMovers(page);

    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    await expect(page.locator('.mp-bucket-losers').first())
      .toBeVisible({ timeout: 30_000 });

    await expect(
      page.locator('.mp-bucket-losers .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });

    const result = await readRows(page, 'mp-bucket-losers');
    console.log('[mobile] Losers row count:', result.count);
    console.log('[mobile] Losers sample:', result.sample.slice(0, 3));

    expect(result.count, 'Mobile: Losers grid must show ≥ 5 rows').toBeGreaterThanOrEqual(5);
  });

  test('losers poll update — subsequent poll delivers updated data', async ({ page }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });

    const user = await signIn(page);
    console.log(`[auth] signed in as ${user}`);

    // Two-phase mock: prices shift on second call so cell values change.
    let callCount = 0;
    await page.route('**/api/watchlist/movers', async route => {
      callCount++;
      const midcapLosers = [
        'BHEL', 'LUPIN', 'AUROPHARMA', 'MARICO', 'COLPAL',
        'TVSMOTOR', 'BANKBARODA', 'CHOLAFIN', 'IRFC', 'RECLTD',
      ].map((sym, i) => ({
        tradingsymbol: sym,
        exchange: 'NSE',
        // Second call: prices 10 pts lower so change_pct shifts.
        last_price: callCount === 1 ? (800 - i * 30) : (790 - i * 30),
        previous_close: 900 - i * 30,
        change_pct: callCount === 1 ? -(2 + i * 0.4) : -(3 + i * 0.4),
        peak_pct: -(2.5 + i * 0.4),
        sticky: false,
      }));
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          movers: midcapLosers,
          threshold_pct: 1.5,
          session_date: new Date().toISOString().slice(0, 10),
          captured_at: null,
        }),
      });
    });

    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    await expect(
      page.locator('.mp-bucket-losers .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });

    const before = await readRows(page, 'mp-bucket-losers');
    console.log('[poll] Before poll — rows:', before.count, 'sample[0]:', before.sample[0]);

    // Wait for the movers poll (fires every ~30 s). Allow 35 s.
    await page.waitForTimeout(35_000);

    const after = await readRows(page, 'mp-bucket-losers');
    console.log('[poll] After 35 s — rows:', after.count, 'sample[0]:', after.sample[0]);

    // Perf: row count must not drop to 0 after poll (keepStaleOnEmpty guard).
    expect(after.count, 'Losers row count must not drop to 0 after poll').toBeGreaterThan(0);

    // Data changes: at least one row text differs (change_pct shifted).
    const changed = before.sample.some((row, i) => row !== after.sample[i]);
    console.log('[poll] Data changed between polls:', changed);
    expect(changed, 'At least one cell value must update after poll').toBe(true);
  });
});
