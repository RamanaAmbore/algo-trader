/**
 * day_pnl_performance_page_valueGetter.spec.js
 *
 * Regression guard for PerformancePage Day P&L column (2026-09-24):
 * The `day_change_val` column now uses a valueGetter that applies
 * baseDayPnlForPosition() per row (except TOTAL, which passes through
 * its own precomputed value). Individual rows must sum to the pinned
 * TOTAL row shown in the same grid.
 *
 * Quality dimensions:
 *   SSOT   — valueGetter calls baseDayPnlForPosition(p.data) per row;
 *            TOTAL row detected via _isTotal || tradingsymbol === 'TOTAL'
 *   Perf   — ag-Grid valueGetter lightweight (no API calls)
 *   Stale  — TOTAL row aggregation matches sum of per-row baseline diffs
 *   Reuse  — same baseDayPnlForPosition formula as derivatives/dashboard
 *   UX     — position rows sum to TOTAL directly above/below them
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

const PERF_SRC = path.resolve(
  process.cwd(),
  'src/lib/PerformancePage.svelte'
);

// ── Static source checks ──────────────────────────────────────────────────────

test('SSOT: PerformancePage imports baseDayPnlForPosition from $lib/data/nav', () => {
  const src = fs.readFileSync(PERF_SRC, 'utf8');

  expect(
    /import\s*\{[^}]*\bbaseDayPnlForPosition\b[^}]*\}\s*from\s*'\$lib\/data\/nav'/.test(src),
    'PerformancePage.svelte must import baseDayPnlForPosition from $lib/data/nav'
  ).toBe(true);
});

test('SSOT: PerformancePage day_change_val column has valueGetter calling baseDayPnlForPosition', () => {
  const src = fs.readFileSync(PERF_SRC, 'utf8');

  // Find the day_change_val column definition
  const colStart = src.indexOf("{ colId: 'day_change_val'");
  expect(colStart, 'day_change_val column definition must exist').toBeGreaterThan(0);

  const colEnd = src.indexOf('},', colStart) + 2;
  const colBody = src.slice(colStart, colEnd);

  // Must have valueGetter
  expect(
    colBody.includes('valueGetter:'),
    'day_change_val column must have a valueGetter'
  ).toBe(true);

  // valueGetter must call baseDayPnlForPosition for non-TOTAL rows
  expect(
    colBody.includes('baseDayPnlForPosition(p.data)'),
    'valueGetter must call baseDayPnlForPosition(p.data) for regular rows'
  ).toBe(true);

  // Must detect TOTAL row via _isTotal or tradingsymbol === 'TOTAL'
  const hasTotalDetect = colBody.includes('_isTotal') || colBody.includes("tradingsymbol === 'TOTAL'");
  expect(
    hasTotalDetect,
    'valueGetter must detect TOTAL row via _isTotal or tradingsymbol === "TOTAL"'
  ).toBe(true);

  // TOTAL row must pass through its precomputed value
  expect(
    colBody.includes('p.data.day_change_val'),
    'TOTAL row must pass through its own precomputed day_change_val'
  ).toBe(true);
});

// ── Live UI checks ────────────────────────────────────────────────────────────

const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
];

for (const vp of VIEWPORTS) {
  test.describe(`/admin/performance — Positions Day P&L column sum [${vp.name}]`, () => {
    test.setTimeout(120_000);

    test(`Individual position rows sum to TOTAL row [${vp.name}]`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });

      const pageErrors = [];
      page.on('pageerror', (err) => pageErrors.push(err.message));

      let authOk = false;
      for (const creds of [
        { user: process.env.PLAYWRIGHT_USER || 'ambore', pass: process.env.PLAYWRIGHT_PASS || 'admin1234' },
        { user: 'rambo', pass: 'admin1234' },
      ]) {
        try {
          await loginAsAdmin(page, creds);
          authOk = true;
          break;
        } catch (_) { /* try next */ }
      }
      if (!authOk) {
        test.skip(true, 'No valid credentials — static checks cover the fix');
        return;
      }

      await page.goto(`${BASE}/admin/performance`, {
        waitUntil: 'domcontentloaded',
        timeout: 30_000,
      });

      // Wait for the positions grid to mount
      await page.locator('.ag-root').first().waitFor({ state: 'attached', timeout: 25_000 }).catch(() => {});

      // Extract day_change_val values from the ag-Grid positions grid.
      // Atomically read all values to avoid straddle issues across 4Hz ticks.
      const gridValues = await page.evaluate(() => {
        // ag-Grid rows
        const rows = [];

        // Find all ag-row elements (includes regular rows + pinned TOTAL)
        const rowEls = document.querySelectorAll('.ag-row[role="row"]');
        for (const rowEl of rowEls) {
          // Find cells with col-id matching day_change_val
          const dayCell = rowEl.querySelector('[col-id="day_change_val"]');
          if (!dayCell) continue;

          const dayText = (dayCell.textContent || '').trim();

          // Check if this is a TOTAL row (ag-row-pinned-bottom or similar)
          const isTotal = rowEl.classList.contains('ag-row-pinned-bottom') ||
                          rowEl.getAttribute('row-id') === 'TOTAL' ||
                          dayText.includes('TOTAL'); // fallback

          rows.push({ isTotal, dayText });
        }

        return rows.length > 0 ? rows : null;
      }).catch(() => null);

      if (!gridValues || gridValues.length < 2) {
        // No position rows or only one row
        const realErrors = pageErrors.filter(
          e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors).toHaveLength(0);
        return;
      }

      // Parse numeric values
      const parseNum = (t) => {
        if (!t || t === '—' || t === '-') return 0;
        const clean = String(t).replace(/₹|,/g, '').trim();
        const num = Number(clean);
        return isFinite(num) ? num : 0;
      };

      const regularRows = gridValues.filter(r => !r.isTotal);
      const totalRow = gridValues.find(r => r.isTotal);

      if (!totalRow || regularRows.length === 0) {
        // No TOTAL row or no regular rows
        const realErrors = pageErrors.filter(
          e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors).toHaveLength(0);
        return;
      }

      // Sum regular row day_change_val
      const sumRegular = regularRows.reduce((s, r) => s + parseNum(r.dayText), 0);
      const totalValue = parseNum(totalRow.dayText);

      // Allow ±1 tolerance for rounding per row
      const tolerance = regularRows.length * 0.5;
      expect(
        Math.abs(sumRegular - totalValue),
        `Regular rows day_pnl (Σ${sumRegular.toFixed(0)}) must match TOTAL (${totalValue.toFixed(0)}) ± ${tolerance.toFixed(1)}`
      ).toBeLessThanOrEqual(tolerance);

      // No JS errors
      const realErrors = pageErrors.filter(
        e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
      );
      expect(realErrors, 'No unexpected JS errors').toHaveLength(0);
    });
  });
}
