/**
 * Regression spec: Losers grid row count + update cadence.
 *
 * P0 defect (2026-07-01): Losers grid showed 1 row instead of ~10-20.
 * Root cause: buildUnified's "single-place rule" skipped creating a
 * _majorGroup='movers' row for any symbol already in pinned/watchlist.
 * Since all F&O underlyings appear in the pinned watchlist, the losers
 * grid was almost always empty. Fix: remove the `continue` so pinned
 * symbols also get a dedicated movers row (keyed __mov, no collision).
 *
 * Five quality dimensions:
 *   SSOT   — movers grid feeds from the same moversStore as winners
 *   Perf   — initial paint within 60 s; poll fires within 35 s window
 *   Stale  — no duplicate mover rows in the ag-Grid (keys are __mov)
 *   Reuse  — same readRows helper as pulse_winners_losers_verify.spec.js
 *   UX     — row count ≥ 5 on desktop + mobile; at least one numeric
 *            cell carries a minus-sign (valid loser)
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
 * Mock /api/watchlist/movers to return a deterministic fixture:
 *   10 losers + 10 winners, all with tradingsymbols that overlap
 *   the pinned watchlist (RELIANCE, TCS, HDFCBANK …) so the
 *   single-place-rule regression is exercised.
 */
async function mockMovers(page) {
  const winners = [
    'RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK',
    'BHARTIARTL', 'SBIN', 'KOTAKBANK', 'LT', 'AXISBANK',
  ].map((sym, i) => ({
    tradingsymbol: sym,
    exchange: 'NSE',
    last_price: 1000 + i * 50,
    previous_close: 900 + i * 50,
    change_pct: 3 + i * 0.3,
    peak_pct: 3.5 + i * 0.3,
    sticky: false,
  }));

  const losers = [
    'WIPRO', 'HCLTECH', 'SUNPHARMA', 'TITAN', 'M&M',
    'TATAMOTORS', 'ULTRACEMCO', 'POWERGRID', 'NTPC', 'TATASTEEL',
  ].map((sym, i) => ({
    tradingsymbol: sym,
    exchange: 'NSE',
    last_price: 800 - i * 30,
    previous_close: 900 - i * 30,
    change_pct: -(2 + i * 0.4),
    peak_pct: -(2.5 + i * 0.4),
    sticky: false,
  }));

  await page.route('**/api/watchlist/movers', route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        movers: [...winners, ...losers],
        threshold_pct: 1.5,
        session_date: new Date().toISOString().slice(0, 10),
        captured_at: null,
      }),
    })
  );
}

async function signIn(page) {
  for (const user of ['ambore', 'rambo']) {
    try {
      await loginAsAdmin(page, { user, pass: process.env.PLAYWRIGHT_PASS || 'admin1234' });
      return user;
    } catch (_) { /* try next */ }
  }
  throw new Error('Could not sign in as ambore or rambo');
}

test.describe('Pulse: Losers grid row count regression', () => {
  test('desktop — losers grid shows ≥ 5 rows from pinned-overlap symbols', async ({ page }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });

    const user = await signIn(page);
    console.log(`[auth] signed in as ${user}`);

    // Wire mock BEFORE navigation so the initial fetch hits it.
    await mockMovers(page);

    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    // Wait for the Losers section to mount.
    await expect(page.locator('.mp-bucket-losers').first())
      .toBeVisible({ timeout: 30_000 });

    // Wait up to 60 s for rows to appear (movers poll fires every 30 s).
    await expect(
      page.locator('.mp-bucket-losers .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });

    const result = await readRows(page, 'mp-bucket-losers');
    console.log('[desktop] Losers row count:', result.count);
    console.log('[desktop] Losers sample:', result.sample.slice(0, 5));

    // SSOT: ≥ 5 losers from the fixture (10 were sent; all qualify).
    expect(result.count, 'Losers grid must show ≥ 5 rows').toBeGreaterThanOrEqual(5);

    // UX: at least one row text contains a negative percentage marker.
    const hasNegative = result.sample.some(row => /[-−][\d]/.test(row));
    expect(hasNegative, 'At least one loser row must show a negative value').toBe(true);

    // Stale: no duplicate tradingsymbols in the visible rows.
    // Extract the first token of each row (symbol cell comes first).
    const syms = result.sample.map(r => r.split(' | ')[0]);
    const uniqueSets = new Set(syms);
    expect(uniqueSets.size, 'No duplicate symbol rows in Losers grid').toBe(syms.length);

    // Perf: Winners should also have rows (shared movers pipeline, no regressions there).
    const winResult = await readRows(page, 'mp-bucket-winners');
    console.log('[desktop] Winners row count:', winResult.count);
    expect(winResult.count, 'Winners grid must still show rows').toBeGreaterThanOrEqual(5);
  });

  test('mobile portrait — losers grid shows ≥ 5 rows', async ({ page }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 390, height: 844 });

    const user = await signIn(page);
    console.log(`[auth] mobile signed in as ${user}`);

    await mockMovers(page);

    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

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

  test('losers poll update — subsequent poll changes at least one cell value', async ({ page }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });

    const user = await signIn(page);
    console.log(`[auth] signed in as ${user}`);

    // First response: fixture losers at initial prices.
    let callCount = 0;
    await page.route('**/api/watchlist/movers', async route => {
      callCount++;
      const losers = [
        'WIPRO', 'HCLTECH', 'SUNPHARMA', 'TITAN', 'M&M',
        'TATAMOTORS', 'ULTRACEMCO', 'POWERGRID', 'NTPC', 'TATASTEEL',
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
          movers: losers,
          threshold_pct: 1.5,
          session_date: new Date().toISOString().slice(0, 10),
          captured_at: null,
        }),
      });
    });

    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    await expect(
      page.locator('.mp-bucket-losers .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });

    const before = await readRows(page, 'mp-bucket-losers');
    console.log('[poll] Before poll — rows:', before.count, 'sample[0]:', before.sample[0]);

    // Wait for the movers poll (fires every 6 base ticks × 5s = 30s).
    // Allow up to 35 s for the second API response to land.
    await page.waitForTimeout(35_000);

    const after = await readRows(page, 'mp-bucket-losers');
    console.log('[poll] After 35 s — rows:', after.count, 'sample[0]:', after.sample[0]);

    // Reuse: row count stable (not emptied between polls).
    expect(after.count, 'Losers row count must not drop to 0 after poll').toBeGreaterThan(0);

    // Data changes: at least one row text differs (change_pct shifted).
    const changed = before.sample.some((row, i) => row !== after.sample[i]);
    console.log('[poll] Data changed between polls:', changed);
    expect(changed, 'At least one cell value must update after poll').toBe(true);
  });
});
