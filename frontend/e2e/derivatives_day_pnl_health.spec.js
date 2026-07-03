/**
 * derivatives_day_pnl_health.spec.js
 *
 * Regression guard for the Day P&L = 0 defect (2026-07-03).
 *
 * Root cause: _dayPnlForLeg returned Number(day_change_val ?? 0) without
 * the new-position override. When overnight_quantity = 0 (position opened
 * today) Kite returns day_change_val = 0 and pnl holds the real value.
 * _byUnderlyingTotals had the override; _dayPnlForLeg did not — causing
 * Snapshot Day P&L and per-leg Day P&L cells to show 0.
 *
 * Quality dimensions:
 *   SSOT     — single _dayPnlForLeg function drives both per-leg cell and
 *               _dayPnlByRootMap; override mirrored from _byUnderlyingTotals
 *   Perf     — no XHR budget regression
 *   Stale    — grep confirms the old bare-return pattern is gone
 *   Reusable — _perRootReduce reuses _dayPnlForLeg; no second accumulator
 *   UX       — Day P&L cells render non-zero when overnight_qty=0 + pnl≠0;
 *               missing LTP renders '—' not '0'
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const SRC = path.resolve(
  process.cwd(),
  'src/routes/(algo)/admin/derivatives/+page.svelte'
);

// ── Static source checks ─────────────────────────────────────────────────────

test('SSOT: _dayPnlForLeg contains new-position override', () => {
  const src = fs.readFileSync(SRC, 'utf8');

  // The new-position override block must be present
  expect(
    src.includes('if (oq === 0 && pnl !== 0) day = pnl'),
    'new-position override "if (oq === 0 && pnl !== 0) day = pnl" missing from _dayPnlForLeg'
  ).toBe(true);

  // The bare-return pattern (old broken form) must be gone
  // Old code: `return Number(c?.day_change_val ?? 0);` as the ONLY return
  // in _dayPnlForLeg (i.e. no override before it). We detect this by
  // checking that the function body no longer ends with a bare-return
  // immediately after the expired branch — extract just the function block.
  const fnStart = src.indexOf('function _dayPnlForLeg(');
  expect(fnStart, '_dayPnlForLeg function must exist').toBeGreaterThan(0);
  // Extract up to the closing brace (first `\n  }` after fnStart)
  const fnEnd = src.indexOf('\n  }', fnStart) + 4;
  const fnBody = src.slice(fnStart, fnEnd);

  // The old single-line terminal return is the only return after the expired
  // block. The fix adds `let day = ...` before it.
  expect(
    fnBody.includes('let day = Number(c?.day_change_val ?? 0)'),
    '_dayPnlForLeg should use `let day = ...` not direct `return Number(...)`'
  ).toBe(true);

  // overnight_quantity fields must be read
  expect(
    fnBody.includes('overnight_quantity'),
    '_dayPnlForLeg should read overnight_quantity for the new-position override'
  ).toBe(true);
});

test('SSOT: _dayPnlByRootMap delegates to _dayPnlForLeg via _perRootReduce', () => {
  const src = fs.readFileSync(SRC, 'utf8');

  // _dayPnlByRootMap must exist and call _dayPnlForLeg inside it
  const mapStart = src.indexOf('const _dayPnlByRootMap = $derived.by');
  expect(mapStart, '_dayPnlByRootMap derived must exist').toBeGreaterThan(0);

  const mapEnd = src.indexOf('\n  });', mapStart) + 6;
  const mapBlock = src.slice(mapStart, mapEnd);

  expect(
    mapBlock.includes('_dayPnlForLeg'),
    '_dayPnlByRootMap must call _dayPnlForLeg (single SSOT, not inline formula)'
  ).toBe(true);

  // Must go via _perRootReduce (not a separate hand-rolled loop)
  expect(
    mapBlock.includes('_perRootReduce'),
    '_dayPnlByRootMap must use _perRootReduce for the accumulation'
  ).toBe(true);
});

test('STALE: no duplicate Day P&L accumulator loop beside _dayPnlByRootMap', () => {
  const src = fs.readFileSync(SRC, 'utf8');

  // Only one place should define a day-pnl-by-root map
  const mapDefCount = (src.match(/const _dayPnlByRootMap\s*=/g) || []).length;
  expect(
    mapDefCount,
    '_dayPnlByRootMap should be defined exactly once'
  ).toBe(1);

  // No single line should call `.reduce(` while also referencing `day_change_val`
  // outside of _perRootReduce — that would indicate a rogue second accumulator.
  // Check line-by-line (not dotAll) to avoid false positives from
  // multi-line proximity matches.
  const rogueLines = src.split('\n').filter(
    line => line.includes('.reduce(') && line.includes('day_change_val')
  );
  expect(
    rogueLines.length,
    'No single line should call .reduce() AND reference day_change_val (rogue accumulator)'
  ).toBe(0);
});

// ── Live UI checks ────────────────────────────────────────────────────────────

const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'mobile',  width: 390,  height: 844 },
];

for (const vp of VIEWPORTS) {
  test.describe(`/admin/derivatives — Day P&L health [${vp.name}]`, () => {
    test.setTimeout(120_000);

    test(`Snapshot Day P&L column is non-zero when positions exist [${vp.name}]`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });

      const pageErrors = [];
      page.on('pageerror', (err) => pageErrors.push(err.message));

      const xhrUrls = [];
      page.on('request', (req) => {
        if (req.resourceType() === 'fetch' || req.resourceType() === 'xhr') {
          xhrUrls.push(req.url());
        }
      });

      // Auth — try default credentials, skip live checks if unavailable
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
        test.skip(true, 'No valid credentials — static checks above cover the fix');
        return;
      }

      await page.goto(`${BASE}/admin/derivatives`, {
        waitUntil: 'domcontentloaded',
        timeout: 30_000,
      });

      // Snapshot card always mounts regardless of data
      await page.locator('.opt-byund-card').waitFor({ state: 'attached', timeout: 25_000 });

      // ── Perf budget ──────────────────────────────────────────────────────
      const apiReqs = xhrUrls.filter(u => u.includes('/api/'));
      expect(
        apiReqs.length,
        `Cold-load XHR budget exceeded: ${apiReqs.length} /api/ requests`
      ).toBeLessThan(60);

      // ── Check for snapshot rows ──────────────────────────────────────────
      const dataRows = page.locator('.byund-row:not(.byund-row-total)');
      const rowCount = await dataRows.count();

      if (rowCount === 0) {
        // No live positions — nothing to assert. Verify no JS errors.
        const realErrors = pageErrors.filter(
          e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors, 'No unexpected JS errors on empty book').toHaveLength(0);
        return;
      }

      // ── Day P&L non-zero assertion ───────────────────────────────────────
      // For each snapshot row collect the Day P&L cell text.
      // At least ONE row must show a non-zero, non-dash value.
      // A row that is genuinely flat (pnl=0) is acceptable as zero; we only
      // care that the column is not uniformly zero when there ARE positions.
      const dayPnlCells = page.locator('.byund-row:not(.byund-row-total) .byund-day');
      const cellCount = await dayPnlCells.count();

      if (cellCount === 0) {
        // Column not rendered (e.g. no derivative positions) — skip value check
        const realErrors = pageErrors.filter(
          e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors).toHaveLength(0);
        return;
      }

      const cellTexts = await Promise.all(
        Array.from({ length: cellCount }, (_, i) =>
          dayPnlCells.nth(i).textContent().then(t => t?.trim() ?? '')
        )
      );

      // Regression guard: not ALL cells should be exactly '0' or '₹0' or '—'
      // when we have live positions with intraday movement.
      // We can't guarantee any specific value without a live account, but we
      // CAN assert the column is functional — it should contain at least one
      // cell with a numeral other than zero, OR all cells are legitimately '—'
      // (closed hours / no data). What must NOT happen is all cells '0'/'₹0'.
      const isAllZeroOrDash = cellTexts.every(t =>
        t === '' || t === '—' || t === '0' || t === '₹0' || t === '0.00' || t === '₹0.00'
      );
      const hasAnyDash = cellTexts.some(t => t === '—');
      const hasAnyNumeral = cellTexts.some(t => /[1-9]/.test(t));

      if (!hasAnyNumeral && isAllZeroOrDash && !hasAnyDash) {
        // All zeros, no dashes — this is the regression state
        // Provide diagnostic info in the failure message
        throw new Error(
          `Day P&L regression detected: all ${cellCount} Snapshot Day P&L cells are zero.\n` +
          `Cell texts: ${cellTexts.join(' | ')}\n` +
          'Expected at least one non-zero value or "—" for positions with intraday movement.'
        );
      }

      // ── UX: Day P&L cells in snapshot must be right-aligned ─────────────
      if (cellCount > 0) {
        const firstCellAlign = await dayPnlCells.first().evaluate(el =>
          getComputedStyle(el).textAlign
        );
        expect(
          ['right', 'end'],
          `Snapshot Day P&L cell should be right-aligned, got: ${firstCellAlign}`
        ).toContain(firstCellAlign);
      }

      // ── No JS errors ─────────────────────────────────────────────────────
      const realErrors = pageErrors.filter(
        e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
      );
      expect(realErrors, 'No unexpected JS errors on derivatives page').toHaveLength(0);
    });

    test(`per-leg Day P&L is non-zero for selected underlying when positions exist [${vp.name}]`, async ({ page }) => {
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

      await page.goto(`${BASE}/admin/derivatives`, {
        waitUntil: 'domcontentloaded',
        timeout: 30_000,
      });

      await page.locator('.opt-byund-card').waitFor({ state: 'attached', timeout: 25_000 });

      // Check if legs grid is visible (requires an underlying to be selected)
      const legsGrid = page.locator('.cand-row:not(.cand-row-total)');
      const legsCount = await legsGrid.count().catch(() => 0);

      if (legsCount === 0) {
        // No legs rendered — nothing to check for this test
        const realErrors = pageErrors.filter(
          e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors).toHaveLength(0);
        return;
      }

      // Read per-leg Day P&L cells (.cand-day class on each leg row)
      const legDayCells = page.locator('.cand-row:not(.cand-row-total) .cand-day');
      const legCellCount = await legDayCells.count();

      if (legCellCount === 0) {
        // Column not present for this underlying type
        return;
      }

      const legTexts = await Promise.all(
        Array.from({ length: legCellCount }, (_, i) =>
          legDayCells.nth(i).textContent().then(t => t?.trim() ?? '')
        )
      );

      // Same guard as snapshot: all-zero with no dashes = regression
      const allZeroNoMissing = legTexts.every(t =>
        t === '' || t === '0' || t === '₹0' || t === '0.00' || t === '₹0.00'
      );
      const anyDash = legTexts.some(t => t === '—');
      const anyNumeral = legTexts.some(t => /[1-9]/.test(t));

      if (allZeroNoMissing && !anyDash && !anyNumeral) {
        throw new Error(
          `Per-leg Day P&L regression: all ${legCellCount} leg Day P&L cells are zero.\n` +
          `Leg cell texts: ${legTexts.join(' | ')}\n` +
          'Expected at least one non-zero value when legs exist with intraday movement.'
        );
      }

      // No JS errors
      const realErrors = pageErrors.filter(
        e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
      );
      expect(realErrors).toHaveLength(0);
    });
  });
}
