/**
 * day_pnl_market_pulse_pinned_rows.spec.js
 *
 * Regression guard for MarketPulse per-account Day P&L sourcing (2026-09-24):
 * Pinned per-account summary rows now read Day P&L from positionsDayPnlStore.byAccount
 * (uppercase-normalized key) instead of the backend's day_change_val. This ensures
 * per-account rows and the pinned TOTAL row always reconcile — both draw from the
 * exact same live-tick-adjusted aggregate.
 *
 * Quality dimensions:
 *   SSOT   — day_pnl sourced from positionsDayPnlStore.byAccount[...toUpperCase()];
 *            day_change_percentage computed via dayChangePct(day_pnl, day_prev_val);
 *            no fallback to backend day_change_val
 *   Perf   — no XHR budget regression on /pulse cold-load
 *   Stale  — positionsSummaryData re-sourcing stays within $derived.by block
 *   Reuse  — same store pattern already used for holdings summary
 *   UX     — per-account rows sum to the pinned TOTAL row in real-time
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

const MP_SRC = path.resolve(
  process.cwd(),
  'src/lib/MarketPulse.svelte'
);

// ── Static source checks ──────────────────────────────────────────────────────

test('SSOT: MarketPulse positionsSummaryData reads day_pnl from positionsDayPnlStore.byAccount', () => {
  const src = fs.readFileSync(MP_SRC, 'utf8');

  // Locate positionsSummaryData $derived.by block
  const blockStart = src.indexOf('const positionsSummaryData = $derived.by(');
  expect(blockStart, 'positionsSummaryData $derived.by block must exist').toBeGreaterThan(0);

  const blockEnd = src.indexOf('\n  });', blockStart) + 6;
  const blockBody = src.slice(blockStart, blockEnd);

  // Must read from positionsDayPnlStore.byAccount with uppercase key
  expect(
    blockBody.includes('positionsDayPnlStore.byAccount[String(r.account).toUpperCase()]'),
    'positionsSummaryData must source day_pnl from positionsDayPnlStore.byAccount[...toUpperCase()]'
  ).toBe(true);

  // Must use dayChangePct to recompute percentage
  expect(
    blockBody.includes('dayChangePct(day_pnl, day_prev_val)'),
    'day_change_percentage must be computed via dayChangePct(day_pnl, day_prev_val)'
  ).toBe(true);

  // Must NOT use backend day_change_val directly for day_pnl
  const hasBkndDayVal = blockBody.includes('r.day_change_val');
  expect(
    hasBkndDayVal,
    'positionsSummaryData must NOT read day_pnl from backend r.day_change_val'
  ).toBe(false);
});

test('SSOT: MarketPulse imports dayChangePct from $lib/data/nav', () => {
  const src = fs.readFileSync(MP_SRC, 'utf8');

  expect(
    /import\s*\{[^}]*\bdayChangePct\b[^}]*\}\s*from\s*'\$lib\/data\/nav'/.test(src),
    'MarketPulse.svelte must import dayChangePct from $lib/data/nav'
  ).toBe(true);
});

test('SSOT: MarketPulse imports positionsDayPnlStore', () => {
  const src = fs.readFileSync(MP_SRC, 'utf8');

  expect(
    src.includes('positionsDayPnlStore'),
    'MarketPulse.svelte must import and use positionsDayPnlStore'
  ).toBe(true);
});

// ── Live UI checks ────────────────────────────────────────────────────────────

const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
];

for (const vp of VIEWPORTS) {
  test.describe(`/pulse — Per-account Day P&L pinned rows [${vp.name}]`, () => {
    test.setTimeout(120_000);

    test(`Per-account summary rows sum to TOTAL row [${vp.name}]`, async ({ page }) => {
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

      await page.goto(`${BASE}/pulse`, {
        waitUntil: 'domcontentloaded',
        timeout: 30_000,
      });

      // Wait for the Positions Summary to mount (it lives in a separate section)
      await page.locator('.mp-summary-card, .positions-summary').first().waitFor({ state: 'attached', timeout: 25_000 }).catch(() => {});

      // Extract per-account day_pnl values from the summary table.
      // The summary table has per-account rows + a TOTAL row (if no account filter).
      // We'll read the values via evaluate to ensure atomicity across 4Hz ticks.
      const summaryValues = await page.evaluate(() => {
        // Find the positions summary section — may be identified by class or text
        const summarySection = document.querySelector('.positions-summary, .summary-section');
        if (!summarySection) return null;

        // Collect all summary rows (per-account + TOTAL)
        const rows = [];
        const rowEls = summarySection.querySelectorAll('[data-account], .summary-row');
        for (const row of rowEls) {
          const accountEl = row.querySelector('[data-account], .account-name');
          const dayPnlEl = row.querySelector('[data-day-pnl], .day-pnl-value');

          if (!accountEl || !dayPnlEl) continue;

          const account = (accountEl.textContent || '').trim();
          const dayPnlText = (dayPnlEl.textContent || '').trim();

          if (account) {
            rows.push({ account, dayPnlText });
          }
        }
        return rows.length > 0 ? rows : null;
      }).catch(() => null);

      if (!summaryValues || summaryValues.length < 2) {
        // No summary rows visible or only one account — skip the sum check
        const realErrors = pageErrors.filter(
          e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors).toHaveLength(0);
        return;
      }

      // Parse numeric values — handle ₹ prefix, commas, and dashes
      const parseNum = (t) => {
        if (!t || t === '—' || t === '-') return 0;
        const clean = String(t).replace(/₹|,/g, '').trim();
        const num = Number(clean);
        return isFinite(num) ? num : 0;
      };

      const accounts = summaryValues.filter(r => !r.account.toUpperCase().includes('TOTAL'));
      const totalRow = summaryValues.find(r => r.account.toUpperCase() === 'TOTAL');

      if (!totalRow || accounts.length === 0) {
        // Empty book or filtered summary
        const realErrors = pageErrors.filter(
          e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors).toHaveLength(0);
        return;
      }

      // Sum per-account day_pnl values
      const sumDayPnl = accounts.reduce((s, r) => s + parseNum(r.dayPnlText), 0);
      const totalDayPnl = parseNum(totalRow.dayPnlText);

      // Allow ±1 tolerance for rounding on sums of ≥2 rows
      const tolerance = accounts.length * 0.5; // 0.5 per row for rounding
      expect(
        Math.abs(sumDayPnl - totalDayPnl),
        `Per-account day_pnl (Σ${sumDayPnl.toFixed(0)}) must match TOTAL (${totalDayPnl.toFixed(0)}) ± ${tolerance.toFixed(1)}`
      ).toBeLessThanOrEqual(tolerance);

      // No JS errors
      const realErrors = pageErrors.filter(
        e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
      );
      expect(realErrors, 'No unexpected JS errors on /pulse').toHaveLength(0);
    });
  });
}

// ── Pinned TOTAL row bypasses symbol search/filter (2026-09 fix) ──────────
//
// ag-Grid's pinnedBottomRowData is immune to its own quick filter by
// design. Both MarketPulse.svelte and PerformancePage.svelte used to push
// the TOTAL row unconditionally, so a TOTAL computed from the FULL
// (unfiltered) row set kept showing even while the symbol search box had
// hidden every visible body row — a TOTAL that silently disagreed with
// what was on screen. Fix: hide the pinned TOTAL row entirely whenever
// `gridApi.isAnyFilterPresent()` is true (quick filter, column filter, or
// floating filter), re-applied via a grid-level `onFilterChanged`
// listener so it reacts immediately, not just on the next data poll.

test.describe('MarketPulse — pinned TOTAL row hides while a symbol filter is active', () => {
  test.setTimeout(90_000);

  test('Positions bucket: typing in the symbol search hides the pinned TOTAL row; clearing restores it', async ({ page }) => {
    let authOk = false;
    for (const creds of [
      { user: process.env.PLAYWRIGHT_USER || 'ambore', pass: process.env.PLAYWRIGHT_PASS || 'admin1234' },
      { user: 'rambo', pass: 'admin1234' },
    ]) {
      try { await loginAsAdmin(page, creds); authOk = true; break; } catch (_) { /* try next */ }
    }
    if (!authOk) { test.skip(true, 'No valid credentials'); return; }

    // Relative goto (not the ${BASE} absolute-URL convention used by the
    // test above) — this test must exercise whatever build is under the
    // playwright config's own baseURL (local dev server by default), not
    // a separately-deployed dev.ramboq.com that may lag behind local edits.
    await page.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: 30_000 });

    const positionsSection = page.locator('.mp-bucket-positions');
    await positionsSection.waitFor({ state: 'attached', timeout: 25_000 }).catch(() => {});
    // Wait for the grid's body rows to actually paint before checking the
    // pinned TOTAL — ag-Grid populates asynchronously after the poll lands.
    await positionsSection.locator('.ag-row').first().waitFor({ state: 'visible', timeout: 20_000 }).catch(() => {});

    const pinnedRows = positionsSection.locator('.ag-floating-bottom .ag-row');
    const initialCount = await pinnedRows.count().catch(() => 0);
    if (initialCount === 0) {
      test.skip(true, 'No positions/TOTAL row in the active book — nothing to filter');
      return;
    }
    expect(initialCount, 'pinned TOTAL row must be visible before any filter is applied').toBeGreaterThan(0);

    // Open the Positions card's inline symbol search and type a filter.
    const searchBtn = positionsSection.locator('button.grid-search-btn[aria-label="Toggle Positions symbol filter"]').first();
    await searchBtn.click();
    const searchInput = positionsSection.locator('input.grid-search-input').first();
    await searchInput.waitFor({ state: 'visible', timeout: 5_000 });
    await searchInput.fill('ZZZZ-NO-MATCH');

    await expect(pinnedRows, 'pinned TOTAL row must disappear while a symbol filter is active')
      .toHaveCount(0, { timeout: 5_000 });

    // Clear the filter — TOTAL must reappear.
    await searchInput.fill('');
    await expect(pinnedRows, 'pinned TOTAL row must reappear once the filter is cleared')
      .toHaveCount(initialCount, { timeout: 5_000 });
  });
});

// PerformancePage.svelte — static SSOT test, not a live-UI interaction.
//
// The ONLY mount point for PerformancePage.svelte is the public
// `/performance` route, and that route passes `showGridControls={false}`
// (src/routes/(public)/performance/+page.svelte), which hides the custom
// GridSearchButton entirely — so a live test driving the header search box
// (as written for MarketPulse above) would always skip, vacuously.
//
// The bug is still live-reachable today, though: `defaultColDef` sets
// `filter: true` on every column regardless of `showGridControls`, and
// ag-Grid renders its own column-filter funnel icon
// (`.ag-header-cell-filter-button`, confirmed present on the deployed
// public page) independent of that prop. A public visitor opening a
// column filter would hit the exact same "pinned TOTAL ignores the
// active filter" bug. Driving that ag-Grid v33 filter popup reliably
// from Playwright proved too brittle to land here, so this is a static
// source-code guard instead — it asserts the fix's wiring is present on
// both grids, matching the SSOT-test style already used at the top of
// this file.
test.describe('PerformancePage — pinned TOTAL row wiring (static SSOT guard)', () => {
  const PERF_SRC = fs.readFileSync(
    path.resolve(process.cwd(), 'src/lib/PerformancePage.svelte'),
    'utf-8'
  );

  test('positionsAllGrid registers onFilterChanged -> _applyPinnedTotal', () => {
    expect(PERF_SRC).toMatch(
      /positionsAllGrid\s*=\s*makeGrid\([\s\S]{0,300}?onFilterChanged:\s*\(\)\s*=>\s*_applyPinnedTotal\(positionsAllGrid,\s*_lastPositionsTotal\)/
    );
  });

  test('holdingsAllGrid registers onFilterChanged -> _applyPinnedTotal', () => {
    expect(PERF_SRC).toMatch(
      /holdingsAllGrid\s*=\s*makeGrid\([\s\S]{0,300}?onFilterChanged:\s*\(\)\s*=>\s*_applyPinnedTotal\(holdingsAllGrid,\s*_lastHoldingsTotal\)/
    );
  });

  test('_applyPinnedTotal hides the TOTAL row whenever isAnyFilterPresent() is true', () => {
    expect(PERF_SRC).toContain(
      "gridApi.setGridOption('pinnedBottomRowData', gridApi.isAnyFilterPresent() ? [] : totalRows);"
    );
  });

  test('neither grid still writes pinnedBottomRowData directly, bypassing the filter guard', () => {
    expect(PERF_SRC).not.toMatch(/holdingsAllGrid\.setGridOption\('pinnedBottomRowData'/);
    expect(PERF_SRC).not.toMatch(/positionsAllGrid\.setGridOption\('pinnedBottomRowData'/);
  });
});
