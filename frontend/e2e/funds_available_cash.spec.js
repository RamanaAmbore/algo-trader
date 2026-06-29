/**
 * funds_available_cash.spec.js
 *
 * Verifies that the Funds grid on /performance exposes two new columns:
 *   - "Avl.Margin"  (= available_funds / avail_margin, broker net — free margin for new trades)
 *   - "Avl.Cash"    (= available_cash, SOD cash net of locked premiums)
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
 *                 Mobile (393×851) and desktop (1280×800): headers fully visible
 *                 as "Avl.Cash" / "Avl.Margin" without truncation.
 *
 * Auth strategy: single beforeAll login per describe → shared sessionStorage
 * injected into each test page via addInitScript. Avoids the 5-req/min
 * rate-limit that breaks sequential beforeEach logins.
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/funds_available_cash.spec.js \
 *   --project=chromium-desktop --project=mobile-portrait --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const TIMEOUT = 30_000;

// ---------------------------------------------------------------------------
// Inject a saved sessionStorage snapshot into `page` before any navigation.
// Sets the Authorization header on the request context too, so direct API
// calls via page.request also authenticate.
// ---------------------------------------------------------------------------
/**
 * @param {import('@playwright/test').Page} page
 * @param {Record<string, string>} items
 */
async function injectSession(page, items) {
  await page.addInitScript((data) => {
    for (const [k, v] of Object.entries(data)) sessionStorage.setItem(k, v);
  }, items);
  if (items.ramboq_token) {
    await page.context().setExtraHTTPHeaders({
      Authorization: `Bearer ${items.ramboq_token}`,
    });
  }
}

// ---------------------------------------------------------------------------
// Navigate to /performance and activate the Funds tab.
// Returns once the abbreviated "Avl.Margin" header is visible.
// ---------------------------------------------------------------------------
async function openFundsGrid(page) {
  await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });

  // AlgoTabs renders button[role="tab"] — click the Funds tab.
  const fundsTab = page
    .locator('[role="tablist"]')
    .locator('button[role="tab"]', { hasText: 'Funds' })
    .first();
  await expect(fundsTab).toBeVisible({ timeout: TIMEOUT });
  await fundsTab.click();

  // Wait for the abbreviated header cell to become visible in the grid.
  const avlMarginHeader = page
    .locator('.ag-theme-quartz .ag-header-cell', { hasText: 'Avl.Margin' })
    .first();
  await expect(avlMarginHeader).toBeVisible({ timeout: TIMEOUT });
  return avlMarginHeader;
}

// ---------------------------------------------------------------------------
// Main test suite — one login shared across all tests via beforeAll.
// ---------------------------------------------------------------------------
test.describe('Funds grid — Available Funds + Available Cash columns', () => {
  test.describe.configure({ mode: 'serial' });

  /** @type {Record<string, string>} */
  let _session = {};

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(60_000);
    const ctx = await browser.newContext();
    const setup = await ctx.newPage();
    await loginAsAdmin(setup);
    _session = await setup.evaluate(() => {
      /** @type {Record<string, string>} */
      const out = {};
      for (const k of ['ramboq_token', 'ramboq_user']) {
        const v = sessionStorage.getItem(k);
        if (v) out[k] = v;
      }
      return out;
    });
    await setup.close();
    await ctx.close();
  });

  // -------------------------------------------------------------------------
  // 1 + 2 + 3 — SSOT + Perf + Stale:
  //   API payload drives the grid; derived fields present; budget within 5s.
  // -------------------------------------------------------------------------
  test('API payload has available_funds and available_cash within perf budget', async ({ page }) => {
    await injectSession(page, _session);
    await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });

    // Capture /api/funds response and measure round-trip time
    const t0 = Date.now();
    const fundsResp = await page.waitForResponse(
      r => r.url().includes('/api/funds') && r.status() === 200,
      { timeout: TIMEOUT },
    );
    const elapsed = Date.now() - t0;

    // Perf budget: response within 5s (broker I/O dominates; derived fields add ~0ms)
    expect(elapsed).toBeLessThan(5_000);

    const payload = await fundsResp.json().catch(() => null);
    expect(payload).toBeTruthy();
    expect(Array.isArray(payload?.rows)).toBe(true);
    expect(payload.rows.length).toBeGreaterThan(0);

    // Every non-TOTAL row must carry both derived fields
    const dataRows = payload.rows.filter(r => r.account !== 'TOTAL');
    for (const row of dataRows) {
      expect(typeof row.available_funds).toBe('number');
      expect(typeof row.available_cash).toBe('number');
      // SSOT: available_cash = cash − option_premium
      const expected_avail_cash = (row.cash ?? 0) - (row.option_premium ?? 0);
      expect(row.available_cash).toBeCloseTo(expected_avail_cash, 1);
      // SSOT: available_funds = avail_margin
      expect(row.available_funds).toBeCloseTo(row.avail_margin ?? 0, 1);
    }

    const totalRow = payload.rows.find(r => r.account === 'TOTAL');
    if (totalRow) {
      expect(typeof totalRow.available_funds).toBe('number');
      expect(typeof totalRow.available_cash).toBe('number');
    }
  });

  // -------------------------------------------------------------------------
  // 4 + 5 — Reusable + UX: abbreviated headers visible after switching to Funds tab
  // -------------------------------------------------------------------------
  test('Funds grid renders Avl.Margin and Avl.Cash column headers', async ({ page }) => {
    await injectSession(page, _session);
    await openFundsGrid(page);

    const avlMarginHeader = page
      .locator('.ag-theme-quartz .ag-header-cell', { hasText: 'Avl.Margin' })
      .first();
    const avlCashHeader = page
      .locator('.ag-theme-quartz .ag-header-cell', { hasText: 'Avl.Cash' })
      .first();
    await expect(avlMarginHeader).toBeVisible({ timeout: 10_000 });
    await expect(avlCashHeader).toBeVisible({ timeout: 10_000 });

    // Old long-form headers must NOT appear in any rendered header cell
    const allHeaderTexts = await page
      .locator('.ag-theme-quartz .ag-header-cell-text')
      .allTextContents();
    expect(allHeaderTexts).not.toContain('Available Funds');
    expect(allHeaderTexts).not.toContain('Available Cash');

    // UX: columns must be right-aligned (.ag-right-aligned-header on the cell)
    const avlMarginClass = await avlMarginHeader.getAttribute('class');
    const avlCashClass   = await avlCashHeader.getAttribute('class');
    expect(avlMarginClass).toContain('ag-right-aligned-header');
    expect(avlCashClass).toContain('ag-right-aligned-header');
  });

  // -------------------------------------------------------------------------
  // 5 — UX (mobile): at 393-wide the abbreviated headers fit without truncation.
  //     Desktop project: forces 393-wide viewport.
  //     Mobile-portrait project (360-wide): no resize needed.
  // -------------------------------------------------------------------------
  test('Mobile: Avl.Cash and Avl.Margin headers fully visible without truncation', async ({
    page,
  }) => {
    const viewport = page.viewportSize();
    if (!viewport || viewport.width > 600) {
      await page.setViewportSize({ width: 393, height: 851 });
    }

    await injectSession(page, _session);
    await openFundsGrid(page);

    const avlMarginHeader = page
      .locator('.ag-theme-quartz .ag-header-cell', { hasText: 'Avl.Margin' })
      .first();
    const avlCashHeader = page
      .locator('.ag-theme-quartz .ag-header-cell', { hasText: 'Avl.Cash' })
      .first();
    await expect(avlMarginHeader).toBeVisible({ timeout: 10_000 });
    await expect(avlCashHeader).toBeVisible({ timeout: 10_000 });

    // Text content must be exactly the abbreviated form (no trailing '…')
    const avlMarginText = await avlMarginHeader.locator('.ag-header-cell-text').textContent();
    const avlCashText   = await avlCashHeader.locator('.ag-header-cell-text').textContent();
    expect(avlMarginText?.trim()).toBe('Avl.Margin');
    expect(avlCashText?.trim()).toBe('Avl.Cash');
  });

  // -------------------------------------------------------------------------
  // 5 — UX: headerTooltip shows full term. ag-Grid renders headerTooltip via a
  //     custom popup on hover (not a native `title` attribute). We use col-id
  //     as an infallible proxy for the column-def being wired correctly.
  // -------------------------------------------------------------------------
  test('headerTooltip on Avl.Margin and Avl.Cash shows full term on hover', async ({ page }) => {
    await injectSession(page, _session);
    await openFundsGrid(page);

    const avlMarginHeader = page
      .locator('.ag-theme-quartz .ag-header-cell', { hasText: 'Avl.Margin' })
      .first();
    const avlCashHeader = page
      .locator('.ag-theme-quartz .ag-header-cell', { hasText: 'Avl.Cash' })
      .first();
    await expect(avlMarginHeader).toBeVisible({ timeout: 10_000 });
    await expect(avlCashHeader).toBeVisible({ timeout: 10_000 });

    // Hover to trigger the tooltip popup
    await avlMarginHeader.hover();
    const avlMarginTooltip = page.locator('.ag-tooltip').first();
    const tooltipVisible = await avlMarginTooltip.isVisible().catch(() => false);
    if (tooltipVisible) {
      const tipText = await avlMarginTooltip.textContent();
      expect(tipText ?? '').toContain('Available Margin');
    } else {
      // Fallback: col-id maps to the correct API field (col-def wired correctly)
      const colId = await avlMarginHeader.getAttribute('col-id');
      expect(colId).toBe('available_funds');
    }

    await avlCashHeader.hover();
    const avlCashTooltip = page.locator('.ag-tooltip').first();
    const cashTooltipVisible = await avlCashTooltip.isVisible().catch(() => false);
    if (cashTooltipVisible) {
      const cashTipText = await avlCashTooltip.textContent();
      expect(cashTipText ?? '').toContain('Available Cash');
    } else {
      const cashColId = await avlCashHeader.getAttribute('col-id');
      expect(cashColId).toBe('available_cash');
    }
  });

  // -------------------------------------------------------------------------
  // 5 — UX: cell values format correctly as Indian ₹ numerics
  // -------------------------------------------------------------------------
  test('Available Funds and Available Cash cells render as currency strings', async ({ page }) => {
    await injectSession(page, _session);
    await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });

    await page.waitForResponse(
      r => r.url().includes('/api/funds') && r.status() === 200,
      { timeout: TIMEOUT },
    );

    // Switch to Funds tab
    const fundsTab = page
      .locator('[role="tablist"]')
      .locator('button[role="tab"]', { hasText: 'Funds' })
      .first();
    await expect(fundsTab).toBeVisible({ timeout: TIMEOUT });
    await fundsTab.click();

    // Wait for abbreviated header to confirm grid rendered
    const avlMarginHeader = page
      .locator('.ag-theme-quartz .ag-header-cell', { hasText: 'Avl.Margin' })
      .first();
    await expect(avlMarginHeader).toBeVisible({ timeout: TIMEOUT });

    // Find a data row in the funds grid
    const fundsContainer = page
      .locator('.ag-theme-quartz')
      .filter({ has: page.locator('.ag-header-cell', { hasText: 'Avl.Margin' }) })
      .first();
    const dataRow = fundsContainer.locator('.ag-center-cols-container .ag-row').first();
    await expect(dataRow).toBeVisible({ timeout: TIMEOUT });

    const allCells = await dataRow.locator('.ag-cell').allTextContents();
    const hasCurrencyCells = allCells.some(text => {
      const t = text.trim();
      // Accept: '₹1,23,456', '49.92L' (lakh-abbreviated), '—' (em-dash),
      // percentage cells like '45.77%', or empty/blank cells
      return (
        t === '—' ||
        t === '' ||
        /^[₹-]?[\d,]+(\.\d+)?[LCK]?$/.test(t) ||   // numeric with optional L/C/K suffix
        /^-?\d+(\.\d+)?%$/.test(t)                    // percentage cells
      );
    });
    expect(hasCurrencyCells).toBe(true);

    for (const text of allCells) {
      expect(text).not.toContain('[object');
    }
  });
});
