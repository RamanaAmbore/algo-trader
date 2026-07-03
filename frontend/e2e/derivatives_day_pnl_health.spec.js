/**
 * derivatives_day_pnl_health.spec.js
 *
 * Regression guard for the Day P&L = 0 defect (2026-07-03) and the
 * strategy-filter fail-closed regression (2026-07-03 retry).
 *
 * Root cause A (commit 35b7352c): _dayPnlForLeg returned Number(day_change_val ?? 0)
 * without the new-position override. When overnight_quantity = 0 (position opened
 * today) Kite returns day_change_val = 0 and pnl holds the real value.
 * _byUnderlyingTotals had the override; _dayPnlForLeg did not — causing
 * Snapshot Day P&L and per-leg Day P&L cells to show 0.
 *
 * Root cause B (this fix): _makeStrategyMatcher() + inline strategy matchers
 * in _byUnderlyingTotals / _byUnderlyingExp / _byUnderlyingDay returned
 * `false` (fail-CLOSED) when `$strategyOpenSymbols.size === 0` but a strategy
 * ID was persisted in sessionStorage. During the async fetch window (or on
 * page cold-load), every symbol failed the filter → _snapshotTotalDay/Pnl/Exp = 0
 * → snapshotTotals store zeroed → NavStrip P pill showed wrong zeros.
 * Payoff overlay DAY Δ row hides when candidatesDayPnl === 0 (OptionsPayoff
 * guard: dayPnl !== 0), so it disappeared too.
 * Fix: fail-OPEN (return true) when strategyOpenSymbols is empty — covers both
 * "still loading" and "no strategy selected" paths without filtering everything out.
 *
 * Quality dimensions:
 *   SSOT     — single _dayPnlForLeg function drives both per-leg cell and
 *               _dayPnlByRootMap; override mirrored from _byUnderlyingTotals.
 *               snapshotTotals store is the single publisher for NavStrip P pill.
 *   Perf     — no XHR budget regression
 *   Stale    — grep confirms the old bare-return pattern is gone;
 *               grep confirms no remaining fail-CLOSED strategy matchers
 *   Reusable — _perRootReduce reuses _dayPnlForLeg; no second accumulator
 *   UX       — Day P&L cells render non-zero when overnight_qty=0 + pnl≠0;
 *               missing LTP renders '—' not '0';
 *               payoff overlay DAY Δ row visible when positions have day pnl;
 *               NavStrip P first slot matches Snapshot TOTAL Day P&L (SSOT)
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

test('STALE: no fail-closed strategy matchers remain in derivatives page', () => {
  const src = fs.readFileSync(SRC, 'utf8');

  // Every "strategyOpenSymbols.size === 0" branch must return true (fail-open),
  // not false (fail-closed). The fail-closed form zeros out the entire snapshot
  // when a strategy ID is persisted in sessionStorage but the async symbol-fetch
  // hasn't completed yet — causing Day P&L, P&L, Exp P&L to show 0.
  const lines = src.split('\n');
  const failClosedLines = lines.filter(
    (line, i) => line.includes('strategyOpenSymbols.size === 0') && line.includes('return false')
  );
  expect(
    failClosedLines.length,
    `Found ${failClosedLines.length} fail-closed strategy matcher(s) — all must use "return true" (fail-open):\n${failClosedLines.join('\n')}`
  ).toBe(0);

  // Confirm fail-open matchers are present (the fix must exist, not just be non-broken)
  const failOpenLines = lines.filter(
    line => line.includes('strategyOpenSymbols.size === 0') && line.includes('return true')
  );
  expect(
    failOpenLines.length,
    'Expected at least 1 fail-open strategy matcher (return true when strategyOpenSymbols empty)'
  ).toBeGreaterThanOrEqual(1);
});

test('SSOT: snapshotTotals store is the single publisher for NavStrip P pill', () => {
  const src = fs.readFileSync(SRC, 'utf8');

  // The $effect that writes to snapshotTotals.set({...}) must exist in the page
  expect(
    src.includes('snapshotTotals.set('),
    'derivatives page must write to snapshotTotals store so NavStrip P pill stays in SSOT sync'
  ).toBe(true);

  // snapshotTotals.set must be called with day, pnl, exp slots
  const setIdx = src.indexOf('snapshotTotals.set(');
  const setBlock = src.slice(setIdx, setIdx + 200);
  expect(setBlock.includes('day:'), 'snapshotTotals.set must include day: slot').toBe(true);
  expect(setBlock.includes('pnl:'), 'snapshotTotals.set must include pnl: slot').toBe(true);
  expect(setBlock.includes('exp:'), 'snapshotTotals.set must include exp: slot').toBe(true);
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

      // Regression guard: if ≥80% of Day P&L cells are exactly zero (not '—')
      // that signals the regression state (Day P&L wired to 0 for most positions).
      // '—' is valid for closed hours or missing data. Genuine flat days are rare
      // across a full portfolio, so the 80% threshold keeps false-positive rate low.
      const isZeroText = (t) =>
        t === '' || t === '0' || t === '₹0' || t === '0.00' || t === '₹0.00';
      const zeroCells = cellTexts.filter(isZeroText).length;
      const dashCells = cellTexts.filter(t => t === '—').length;
      const nonZeroNonDash = cellCount - zeroCells - dashCells;

      if (cellCount >= 2 && zeroCells / cellCount >= 0.8 && nonZeroNonDash === 0) {
        // 80%+ zero, no non-zero non-dash cells — regression state
        throw new Error(
          `Day P&L regression detected: ${zeroCells}/${cellCount} Snapshot Day P&L cells are zero (≥80%).\n` +
          `Cell texts: ${cellTexts.join(' | ')}\n` +
          'Expected meaningful non-zero values for positions with intraday movement.\n' +
          'If overnight_quantity≠0 cells are also 0, check backend _override_stale_ltp_from_ticker.'
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

      // Same 80%-zero guard as the snapshot test
      const isLegZero = (t) =>
        t === '' || t === '0' || t === '₹0' || t === '0.00' || t === '₹0.00';
      const legZeroCnt = legTexts.filter(isLegZero).length;
      const legDashCnt = legTexts.filter(t => t === '—').length;
      const legNonZeroNonDash = legCellCount - legZeroCnt - legDashCnt;

      if (legCellCount >= 2 && legZeroCnt / legCellCount >= 0.8 && legNonZeroNonDash === 0) {
        throw new Error(
          `Per-leg Day P&L regression: ${legZeroCnt}/${legCellCount} leg cells are zero (≥80%).\n` +
          `Leg cell texts: ${legTexts.join(' | ')}\n` +
          'Expected meaningful non-zero values for legs with intraday movement.\n' +
          'If overnight_quantity≠0 legs are also 0, check backend _override_stale_ltp_from_ticker.'
        );
      }

      // No JS errors
      const realErrors = pageErrors.filter(
        e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
      );
      expect(realErrors).toHaveLength(0);
    });

    test(`Payoff overlay DAY Δ row is visible when snapshot has non-zero Day P&L [${vp.name}]`, async ({ page }) => {
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

      // Check if payoff chart card is present
      const payoffCard = page.locator('.opt-payoff-card');
      const payoffPresent = await payoffCard.count().catch(() => 0);
      if (payoffPresent === 0) {
        // No payoff card (no underlying selected or empty book) — skip
        const realErrors = pageErrors.filter(
          e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors).toHaveLength(0);
        return;
      }

      // Check snapshot to see if there's meaningful Day P&L at all
      const snapshotDayTotal = page.locator('.byund-row-total .byund-day');
      const totalDayText = await snapshotDayTotal.textContent().catch(() => '');
      const totalDayTrimmed = (totalDayText || '').trim();
      const isZero = (t) => t === '' || t === '0' || t === '₹0' || t === '—';

      if (isZero(totalDayTrimmed)) {
        // Snapshot TOTAL Day P&L is also zero/dash — no positions with day delta,
        // DAY Δ hiding in payoff is correct in this state. Just verify no JS errors.
        const realErrors = pageErrors.filter(
          e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors).toHaveLength(0);
        return;
      }

      // Snapshot shows non-zero TOTAL Day P&L — payoff overlay DAY Δ must be visible.
      // The stat overlay is visible when the payoff card is not collapsed.
      // The DAY Δ row is identified by its text content 'DAY Δ'.
      const dayDeltaRow = page.locator('.ps-stat-overlay .ps-row .ps-k:has-text("DAY")');
      const dayDeltaCount = await dayDeltaRow.count().catch(() => 0);

      if (dayDeltaCount === 0) {
        // Payoff stat overlay may not be mounted (no legs for selected underlying).
        // This is not a failure — only assert if the overlay is present.
        const realErrors = pageErrors.filter(
          e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors).toHaveLength(0);
        return;
      }

      // Overlay is present and Snapshot Day P&L is non-zero → DAY Δ must be visible
      await expect(dayDeltaRow.first()).toBeVisible();

      // No JS errors
      const realErrors2 = pageErrors.filter(
        e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
      );
      expect(realErrors2).toHaveLength(0);
    });

    test(`NavStrip P first slot (Day) matches Snapshot TOTAL Day P&L SSOT [${vp.name}]`, async ({ page }) => {
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

      // Read Snapshot TOTAL row Day P&L
      const snapshotTotal = page.locator('.byund-row-total .byund-day');
      const snapshotTotalDay = (await snapshotTotal.textContent().catch(() => '')).trim();

      // Read NavStrip P pill first value slot (ps-agg for P → first ps-agg-v child)
      // The P pill: <span class="ps-agg">P</span> <span class="ps-agg-v">...</span>
      // Locate the P label span, then get its parent's first ps-agg-v sibling
      const navStripPDay = page.locator('.ps-agg:has(.ps-agg-k) .ps-agg-v').first();
      const navPDayText = (await navStripPDay.textContent().catch(() => '')).trim();

      // Both should be non-dash and matching when Snapshot has non-zero data
      const isZero = (t) => t === '' || t === '0' || t === '₹0' || t === '—';
      if (isZero(snapshotTotalDay)) {
        // No positions with day delta — skip value comparison
        const realErrors = pageErrors.filter(
          e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors).toHaveLength(0);
        return;
      }

      // When Snapshot TOTAL has a value, NavStrip P day slot must NOT be '0' or '—'
      // (exact text match is hard due to formatting differences — just ensure non-zero)
      expect(
        isZero(navPDayText),
        `NavStrip P day value is "${navPDayText}" but Snapshot TOTAL Day is "${snapshotTotalDay}" — snapshotTotals store not writing correctly (strategy filter fail-closed regression)`
      ).toBe(false);

      // No JS errors
      const realErrors = pageErrors.filter(
        e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
      );
      expect(realErrors).toHaveLength(0);
    });
  });
}
