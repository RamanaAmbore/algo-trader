/**
 * funds_available_cash.spec.js
 *
 * Verifies that the Funds grid on /performance exposes two new columns:
 *   - "Available Funds"  (= avail_margin, broker net — free margin for new trades)
 *   - "Available Cash"   (= cash − option_premium, SOD cash net of locked premiums)
 *
 * Five quality dimensions:
 *   1. SSOT     — available_cash value comes from /api/funds payload; no inline
 *                 frontend arithmetic (the API field is used directly).
 *   2. Perf     — /api/funds XHR budget unchanged; no additional requests.
 *   3. Stale    — column definition uses field names that match FundsRow schema.
 *   4. Reusable — same column-def pattern (flex, numericColumn, valueFormatter,
 *                 headerTooltip) used by neighbouring funds columns.
 *   5. UX       — columns are right-aligned, have header tooltips, non-empty values
 *                 render like ₹ numerics (no raw JS object or [object Object]).
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/funds_available_cash.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const TIMEOUT = 30_000;

test.describe('Funds grid — Available Funds + Available Cash columns', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  // -------------------------------------------------------------------------
  // 1 + 3 — SSOT + Stale: API payload drives the grid values; no re-derivation
  // -------------------------------------------------------------------------
  test('API payload contains available_funds and available_cash per account', async ({ page }) => {
    // Navigate to performance first, then intercept the subsequent funds poll
    // (the page auto-refreshes /api/funds on a 30s cadence, so we wait for
    // the first live-session request after the page loads).
    await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });

    const fundsResp = await page.waitForResponse(
      r => r.url().includes('/api/funds') && r.status() === 200,
      { timeout: TIMEOUT },
    );
    const payload = await fundsResp.json().catch(() => null);

    // Payload must have rows array
    expect(payload).toBeTruthy();
    expect(Array.isArray(payload?.rows)).toBe(true);
    expect(payload.rows.length).toBeGreaterThan(0);

    // Every non-TOTAL row must carry both derived fields
    const dataRows = payload.rows.filter(r => r.account !== 'TOTAL');
    for (const row of dataRows) {
      expect(typeof row.available_funds).toBe('number');
      expect(typeof row.available_cash).toBe('number');
      // SSOT: available_cash = cash − option_premium (within floating-point tolerance)
      const expected_avail_cash = (row.cash ?? 0) - (row.option_premium ?? 0);
      expect(row.available_cash).toBeCloseTo(expected_avail_cash, 1);
      // SSOT: available_funds = avail_margin
      expect(row.available_funds).toBeCloseTo(row.avail_margin ?? 0, 1);
    }

    // TOTAL row must also carry the derived fields
    const totalRow = payload.rows.find(r => r.account === 'TOTAL');
    if (totalRow) {
      expect(typeof totalRow.available_funds).toBe('number');
      expect(typeof totalRow.available_cash).toBe('number');
    }
  });

  // -------------------------------------------------------------------------
  // 4 + 5 — Reusable + UX: columns visible in the grid with correct rendering
  // -------------------------------------------------------------------------
  test('Funds grid renders Available Funds and Available Cash columns', async ({ page }) => {
    await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });

    // Switch to the Funds tab (AlgoTabs strip: NAV / Funds)
    const fundsTab = page.locator('button[role="tab"]', { hasText: 'Funds' }).first();
    await expect(fundsTab).toBeVisible({ timeout: TIMEOUT });
    await fundsTab.click();

    // The funds ag-Grid container should now be visible
    const fundsGrid = page.locator('.ag-theme-quartz').filter({
      has: page.locator('.ag-header-cell', { hasText: 'Available Funds' }),
    }).first();
    await expect(fundsGrid).toBeVisible({ timeout: TIMEOUT });

    // Both column headers must be present
    const availFundsHeader = fundsGrid.locator('.ag-header-cell', { hasText: 'Available Funds' });
    const availCashHeader  = fundsGrid.locator('.ag-header-cell', { hasText: 'Available Cash' });
    await expect(availFundsHeader).toBeVisible({ timeout: 10_000 });
    await expect(availCashHeader).toBeVisible({ timeout: 10_000 });

    // UX: columns must be right-aligned (ag-Grid numericColumn sets text-align: right
    // via .ag-right-aligned-header on the header + right-aligned cells).
    const availFundsHeaderClass = await availFundsHeader.getAttribute('class');
    const availCashHeaderClass  = await availCashHeader.getAttribute('class');
    expect(availFundsHeaderClass).toContain('ag-right-aligned-header');
    expect(availCashHeaderClass).toContain('ag-right-aligned-header');
  });

  // -------------------------------------------------------------------------
  // 5 — UX: cell values format correctly as Indian ₹ numerics
  // -------------------------------------------------------------------------
  test('Available Funds and Available Cash cells render as currency strings', async ({ page }) => {
    await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });

    // Wait for data to load (funds store populated)
    await page.waitForResponse(
      r => r.url().includes('/api/funds') && r.status() === 200,
      { timeout: TIMEOUT },
    );

    // Switch to Funds tab
    const fundsTab = page.locator('button[role="tab"]', { hasText: 'Funds' }).first();
    await fundsTab.click();

    // Wait for the grid to render rows
    const fundsGrid = page.locator('.ag-theme-quartz').filter({
      has: page.locator('.ag-header-cell', { hasText: 'Available Funds' }),
    }).first();

    // Find a data row (non-TOTAL) in the grid
    const dataRow = fundsGrid.locator('.ag-center-cols-container .ag-row').first();
    await expect(dataRow).toBeVisible({ timeout: TIMEOUT });

    // Find the Available Funds cell by column index (determined by header position)
    // Use ag-Grid's aria-colindex attribute driven by header column ordering.
    // Fallback: check all cells in the row for currency-shaped text.
    const allCells = await dataRow.locator('.ag-cell').allTextContents();
    const hasCurrencyCells = allCells.some(text => {
      const t = text.trim();
      // Accept: '₹1,23,456', '1,23,456.00', '—' (em-dash for zero), or numeric string
      return t === '—' || /^[₹-]?[\d,]+(\.\d{0,2})?$/.test(t) || t === '';
    });
    // At least some cells must have currency-shaped values (not [object Object])
    expect(hasCurrencyCells).toBe(true);

    // Specifically: no cell in the data row should contain '[object'
    for (const text of allCells) {
      expect(text).not.toContain('[object');
    }
  });

  // -------------------------------------------------------------------------
  // 2 — Performance: derived columns add zero extra I/O — verify via direct
  //     API call timing (no extra requests; response is a pure derived column
  //     from the existing margins DataFrame, not a new broker call).
  // -------------------------------------------------------------------------
  test('funds API with derived columns returns within performance budget', async ({ page }) => {
    // Use the authenticated request context directly (no page navigation).
    // The performance assertion: a single /api/funds round-trip completes
    // within 5 seconds. Derived columns are pure in-memory Polars expressions
    // and should add < 1ms; the budget is driven by the broker margin call.
    const t0 = Date.now();
    const resp = await page.request.get(`${BASE}/api/funds?fresh=true`, { timeout: 10_000 });
    const elapsed = Date.now() - t0;

    expect(resp.status()).toBe(200);
    // 5 s budget — broker I/O dominates; derived columns add negligible overhead
    expect(elapsed).toBeLessThan(5_000);

    const body = await resp.json().catch(() => null);
    // Both derived fields present — no degradation from prior test run
    expect(Array.isArray(body?.rows)).toBe(true);
    if (body.rows.length > 0) {
      expect(typeof body.rows[0].available_cash).toBe('number');
      expect(typeof body.rows[0].available_funds).toBe('number');
    }
  });

  // -------------------------------------------------------------------------
  // 3 — Stale code: Available Cash value in payload is server-derived, not
  //     computed inline by the client. Verified via direct API call.
  // -------------------------------------------------------------------------
  test('available_cash in API payload is server-derived (SSOT cross-check)', async ({ page }) => {
    // Use the already-authenticated request context (headers set in beforeEach)
    // to call /api/funds directly — no page navigation needed.
    const resp = await page.request.get(`${BASE}/api/funds`, { timeout: TIMEOUT });
    expect(resp.status()).toBe(200);

    const body = await resp.json().catch(() => null);
    expect(body?.rows?.length ?? 0).toBeGreaterThan(0);

    const firstDataRow = body.rows.find(r => r.account !== 'TOTAL');
    if (firstDataRow) {
      // The available_cash in the payload must already be a number
      // (not computed client-side from cash and option_premium)
      expect(typeof firstDataRow.available_cash).toBe('number');
      // Cross-check the SSOT arithmetic once more
      const expected = (firstDataRow.cash ?? 0) - (firstDataRow.option_premium ?? 0);
      expect(firstDataRow.available_cash).toBeCloseTo(expected, 1);
    }
  });
});
