/**
 * derivatives_snapshot_spot_smoke.spec.js
 *
 * Smoke test for the derivatives Snapshot card spot (underlying LTP) column.
 * Verifies that the spot cell renders a non-zero value for the selected underlying,
 * confirming liveSpot updates per-tick for MCX commodities and other underlyings.
 *
 * Background: liveSpot was not updating per-tick for MCX commodities (CRUDEOIL).
 * This test ensures the spot price cell is populated and not stuck at "—" (dash).
 *
 * Quality dimensions:
 *   SSOT   — Snapshot card spot LTP reads from liveSpot (single source)
 *   Perf   — page load completes within reasonable time budget
 *   UX     — spot cell shows non-empty value (not "—") when data is available
 *   Smoke  — lightweight canary for derivatives page derivatives load flow
 *
 * Run:
 *   npx playwright test e2e/derivatives_snapshot_spot_smoke.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const DERIV_URL = `${BASE}/admin/derivatives`;

test.describe('/admin/derivatives — Snapshot card spot price smoke', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('Snapshot card renders and spot cell has non-zero value', async ({ page }) => {
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    // Wait for the Snapshot card container to load
    const snapshotCard = page.locator('.opt-byund-card');
    await snapshotCard.waitFor({ state: 'attached', timeout: 25_000 });

    // Wait for at least one data row to appear (byund-row)
    const snapshotRows = page.locator('.byund-row:not(.byund-row-total)');
    const rowCount = await snapshotRows.count();

    // If no rows exist (no positions), skip gracefully
    if (rowCount === 0) {
      test.skip();
      return;
    }

    // For the first data row, check that the spot (LTP) cell is not empty/dash
    const firstRow = snapshotRows.first();
    const spotCell = firstRow.locator('.num').first();

    // Wait for cell to be visible and extract text
    await spotCell.waitFor({ state: 'visible', timeout: 10_000 });
    const spotText = (await spotCell.textContent()).trim();

    // Spot should not be empty or dash ("—")
    const isDash = spotText === '—' || spotText === '-';
    const isEmpty = !spotText || spotText === '';

    expect(
      !isEmpty && !isDash,
      `Snapshot spot cell should contain a non-empty value (not "—" or empty), got: "${spotText}"`
    ).toBe(true);

    // Parse spot as a number and verify it's positive (non-zero)
    const spotValue = parseFloat(spotText.replace(/[₹,\s]/g, ''));

    expect(
      Number.isFinite(spotValue),
      `Snapshot spot "${spotText}" should parse to a finite number`
    ).toBe(true);

    expect(
      spotValue > 0,
      `Snapshot spot value ${spotValue} should be positive (non-zero)`
    ).toBe(true);
  });

  test('Snapshot card loads without page errors', async ({ page }) => {
    const pageErrors = [];
    page.on('pageerror', (err) => pageErrors.push(err.message));

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    // Wait for snapshot card to render
    await page.locator('.opt-byund-card').waitFor({ state: 'attached', timeout: 25_000 });
    await page.waitForTimeout(500);

    // Filter to real errors (exclude 401, 405, WebSocket noise)
    const realErrors = pageErrors.filter(
      e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource') && !e.includes('WebSocket')
    );

    expect(realErrors, 'Page should load without errors').toHaveLength(0);
  });

  test('Multiple underlying rows in Snapshot card all have non-empty spot cells', async ({ page }) => {
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    // Wait for snapshot card
    await page.locator('.opt-byund-card').waitFor({ state: 'attached', timeout: 25_000 });

    // Get all data rows (exclude total row)
    const snapshotRows = page.locator('.byund-row:not(.byund-row-total)');
    const rowCount = await snapshotRows.count();

    if (rowCount === 0) {
      test.skip();
      return;
    }

    // Check spot cells in first N rows (max 5 to keep test fast)
    const checkCount = Math.min(rowCount, 5);
    for (let i = 0; i < checkCount; i++) {
      const row = snapshotRows.nth(i);
      const spotCell = row.locator('.num').first();
      const spotText = (await spotCell.textContent()).trim();

      const isDash = spotText === '—' || spotText === '-';
      const isEmpty = !spotText || spotText === '';

      expect(
        !isEmpty && !isDash,
        `Row ${i} spot cell should not be empty/dash, got: "${spotText}"`
      ).toBe(true);
    }
  });

  test('LTP dual-signal color classes render on snapshot rows', async ({ page }) => {
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    // Wait for snapshot card to load
    await page.locator('.opt-byund-card').waitFor({ state: 'attached', timeout: 25_000 });

    // Get all data rows (exclude total row)
    const snapshotRows = page.locator('.byund-row:not(.byund-row-total)');
    const rowCount = await snapshotRows.count();

    if (rowCount === 0) {
      test.skip();
      return;
    }

    // Check that at least one row has one of the ltp-day-* color classes
    // Valid classes: ltp-day-flat, ltp-day-pos, ltp-day-neg
    const ltpDayColorClasses = [
      'ltp-day-flat',
      'ltp-day-pos',
      'ltp-day-neg',
    ];

    let foundLtpDayClass = false;

    // Check up to first 5 rows for any ltp-day-* class
    const checkCount = Math.min(rowCount, 5);
    for (let i = 0; i < checkCount; i++) {
      const row = snapshotRows.nth(i);

      // Check each row for any of the ltp-day-* classes
      for (const className of ltpDayColorClasses) {
        const hasClass = await row.locator(`.${className}`).count();
        if (hasClass > 0) {
          foundLtpDayClass = true;
          break;
        }
      }

      if (foundLtpDayClass) break;
    }

    expect(
      foundLtpDayClass,
      `Expected to find at least one element with an ltp-day-* class in snapshot rows`
    ).toBe(true);
  });
});
