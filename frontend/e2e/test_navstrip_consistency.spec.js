/**
 * NavStrip (PositionStrip) consistency spec.
 *
 * Verifies that:
 *   - P pill slot 2 (lifetime P&L) matches the Positions TOTAL row P&L in
 *     the MarketPulse positions grid (ag-Grid pinned-bottom TOTAL row).
 *   - P pill slot 1 (today Day P&L) is within 2% of the Positions TOTAL
 *     Day P&L (wider tolerance because slot 1 carries an SSE-tick delta
 *     while the TOTAL row uses the broker snapshot value only).
 *   - H pill slot 2 (holdings value / cur_val) and slot 3 (holdings lifetime
 *     P&L) are consistent with the Holdings grid TOTAL row.
 *
 * Both surfaces source from the same broker snapshot (fetchPositions /
 * fetchHoldings). After the fix that removed the unguarded SSE delta from
 * PositionStrip's lifetime derived values, the sums should match within
 * 0.1% (rounding from aggCompact formatting) when read synchronously
 * after the first data load.
 *
 * Auth strategy: single beforeAll login → shared sessionStorage injected
 * per test, to avoid burning the 5/min rate-limit with repeated form submits.
 *
 * Run:
 *   PLAYWRIGHT_USER=rambo PLAYWRIGHT_PASS=admin1234 \
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test test_navstrip_consistency --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

// ── session injection ─────────────────────────────────────────────────────────

/**
 * Inject saved sessionStorage into a page before navigation so the auth
 * store picks up the token on mount (no repeated form submits).
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

// ── parse aggCompact strings back to raw numbers ──────────────────────────────
// aggCompact(v) output examples: "1.5L", "2.3Cr", "-45.2K", "320", "0"
// We need to reverse this for numeric comparison.
function parseAggCompact(str) {
  if (!str || str === '—' || str === '-') return null;
  const s = str.replace(/[₹,\s]/g, '').trim();
  if (!s) return null;
  const m = s.match(/^(-?[\d.]+)([KLC]r?)?$/i);
  if (!m) return null;
  const num = parseFloat(m[1]);
  const sfx = (m[2] || '').toUpperCase();
  if (sfx === 'K')  return num * 1_000;
  if (sfx === 'L')  return num * 100_000;
  if (sfx === 'CR') return num * 10_000_000;
  return num;
}

// ── helpers ───────────────────────────────────────────────────────────────────

/**
 * Read the three values from a `.ps-agg` pill span.
 * Returns [slot1, slot2, slot3] as raw numbers (null when not found / dash).
 * @param {import('@playwright/test').Page} page
 * @param {number} pillIndex  0-based index among .ps-agg elements (P=0, M=1, C=2, H=3)
 */
async function readPillSlots(page, pillIndex) {
  const vals = await page.locator('.ps-agg').nth(pillIndex).locator('.ps-agg-v').allInnerTexts();
  return vals.map(v => parseAggCompact(v.trim()));
}

/**
 * Read the P&L value from the ag-Grid pinned bottom TOTAL row for a given
 * grid container selector and column field name.
 *
 * ag-Grid renders pinned-bottom rows in `.ag-floating-bottom-container`.
 * The cell's `col-id` attribute equals the field name.
 *
 * @param {import('@playwright/test').Locator} gridContainer
 * @param {string} colId
 */
async function readGridTotalCell(gridContainer, colId) {
  const cell = gridContainer.locator(`.ag-floating-bottom-container .ag-row .ag-cell[col-id="${colId}"]`).first();
  const text = await cell.innerText().catch(() => null);
  if (!text) return null;
  return parseAggCompact(text.trim());
}

// ── tests ─────────────────────────────────────────────────────────────────────

test.describe('NavStrip consistency with MarketPulse TOTAL rows', () => {
  test.describe.configure({ mode: 'serial' });

  /** @type {Record<string, string>} */
  let _session = {};

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(60_000);
    const ctx = await browser.newContext();
    const setup = await ctx.newPage();
    await loginAsAdmin(setup);
    _session = await setup.evaluate(() => {
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

  test('P slot 2 (lifetime P&L) matches Positions grid TOTAL P&L', async ({ page }) => {
    await injectSession(page, _session);
    await page.goto('/pulse');

    // Wait for the NavStrip to appear and have non-zero data.
    const pPill = page.locator('.ps-agg').first();
    await expect(pPill).toBeVisible({ timeout: 20_000 });

    // Wait for positions grid TOTAL row to appear in the ag-Grid.
    const posGrid = page.locator('.mp-bucket-positions').first();
    await expect(
      posGrid.locator('.ag-floating-bottom-container .ag-row').first()
    ).toBeVisible({ timeout: 30_000 });

    // Allow one extra poll cycle so data has settled from the same fetch.
    await page.waitForTimeout(1_500);

    // Read P pill slots [day, lifetime, expiry]
    const [_pDay, pLifetime, _pExpiry] = await readPillSlots(page, 0);
    expect(pLifetime, 'P slot 2 must parse to a number').not.toBeNull();

    // Read Positions TOTAL row 'pnl' cell from the ag-Grid.
    const gridTotalPnl = await readGridTotalCell(posGrid, 'pnl');
    expect(gridTotalPnl, 'TOTAL row P&L cell must parse to a number').not.toBeNull();

    // Tolerance: 0.1% relative — tight enough to catch divergence, loose enough
    // for aggCompact rounding of large numbers. Lifetime P&L has NO SSE delta
    // so this should be exact within rounding.
    const diff = Math.abs(pLifetime - gridTotalPnl);
    const denom = Math.max(Math.abs(pLifetime), Math.abs(gridTotalPnl), 1);
    expect(
      diff / denom,
      `P slot 2 (${pLifetime}) diverges from Positions TOTAL P&L (${gridTotalPnl}) by ${diff}`
    ).toBeLessThanOrEqual(0.001);
  });

  test('P slot 1 (Day P&L) matches Positions grid TOTAL day_pnl', async ({ page }) => {
    await injectSession(page, _session);
    await page.goto('/pulse');

    const pPill = page.locator('.ps-agg').first();
    await expect(pPill).toBeVisible({ timeout: 20_000 });

    const posGrid = page.locator('.mp-bucket-positions').first();
    await expect(
      posGrid.locator('.ag-floating-bottom-container .ag-row').first()
    ).toBeVisible({ timeout: 30_000 });

    await page.waitForTimeout(1_500);

    const [pDay, _pLifetime, _pExpiry] = await readPillSlots(page, 0);
    expect(pDay, 'P slot 1 must parse to a number').not.toBeNull();

    const gridTotalDayPnl = await readGridTotalCell(posGrid, 'day_pnl');
    expect(gridTotalDayPnl, 'TOTAL row day_pnl cell must parse to a number').not.toBeNull();

    const diff  = Math.abs(pDay - gridTotalDayPnl);
    const denom = Math.max(Math.abs(pDay), Math.abs(gridTotalDayPnl), 1);
    // Slot 1 carries an SSE-tick delta on top of the broker snapshot while
    // the MarketPulse TOTAL uses the broker snapshot only. The two can
    // legitimately diverge during active trading (LTPs ticking between polls).
    // 2% tolerance covers reasonable intraday drift without hiding a data-
    // source mismatch (which would be 5–50×+ off).
    expect(
      diff / denom,
      `P slot 1 (${pDay}) diverges from Positions TOTAL Day P&L (${gridTotalDayPnl}) by ${diff}`
    ).toBeLessThanOrEqual(0.02);
  });

  test('H pill slot 2 (holdings value) and slot 3 (lifetime P&L) consistent with Holdings TOTAL', async ({ page }) => {
    await injectSession(page, _session);
    await page.goto('/pulse');

    const hPill = page.locator('.ps-agg').nth(3);  // H is the 4th pill (0-indexed)
    await expect(hPill).toBeVisible({ timeout: 20_000 });

    const holdGrid = page.locator('.mp-bucket-holdings').first();
    // If holdings grid is absent (no holdings), skip gracefully.
    const holdGridExists = await holdGrid.count();
    if (!holdGridExists) {
      test.skip(true, 'No holdings grid visible — skipping H pill check');
      return;
    }

    await expect(
      holdGrid.locator('.ag-floating-bottom-container .ag-row').first()
    ).toBeVisible({ timeout: 30_000 });

    await page.waitForTimeout(1_500);

    // H pill: [today, holdingsValue, holdingsLifetimePnl]
    const [_hDay, hValue, hLifetime] = await readPillSlots(page, 3);
    expect(hValue,    'H slot 2 (value) must parse').not.toBeNull();
    expect(hLifetime, 'H slot 3 (lifetime) must parse').not.toBeNull();

    const gridTotalCurVal = await readGridTotalCell(holdGrid, 'cur_val');
    const gridTotalPnl    = await readGridTotalCell(holdGrid, 'pnl');

    if (gridTotalCurVal != null) {
      const diff  = Math.abs(hValue - gridTotalCurVal);
      const denom = Math.max(Math.abs(hValue), Math.abs(gridTotalCurVal), 1);
      expect(
        diff / denom,
        `H slot 2 (${hValue}) diverges from Holdings TOTAL cur_val (${gridTotalCurVal}) by ${diff}`
      ).toBeLessThanOrEqual(0.001);
    }

    if (gridTotalPnl != null) {
      const diff  = Math.abs(hLifetime - gridTotalPnl);
      const denom = Math.max(Math.abs(hLifetime), Math.abs(gridTotalPnl), 1);
      expect(
        diff / denom,
        `H slot 3 (${hLifetime}) diverges from Holdings TOTAL P&L (${gridTotalPnl}) by ${diff}`
      ).toBeLessThanOrEqual(0.001);
    }
  });
});
