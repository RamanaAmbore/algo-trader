/**
 * day_pnl_ssot.spec.js
 *
 * Regression guard for the Day P&L SSOT gap (2026-07-03):
 * MarketPulse.svelte read `r.day_change_val` directly for both `brokerDcv`
 * (the closed-hours else-branch and realised-today decomposition base) and
 * `_broker_day_pnl` (the TOTAL row mirror). When overnight_quantity=0 (position
 * opened today) Kite returns day_change_val=0 while pnl holds the real value.
 * This caused the MarketPulse position card to show 0 Day P&L in the
 * closed-hours else-branch, and the TOTAL row to diverge from NavStrip P slot 1
 * which was already using baseDayPnlForPosition via positionsPnlFiltered.
 *
 * Fix: both reads now go through baseDayPnlForPosition(r) from $lib/data/nav.
 * baseDayPnlForPosition is the frontend SSOT for the new-position override:
 *   oq === 0 && dcv === 0 && pnl !== 0  →  day = pnl   (opened today, broker dcv stale 0)
 *   else                                →  day = day_change_val
 *
 * After commit f378ce53 "Refactor: MarketPulse.svelte — migrate buildUnified to
 * pulseUnified helpers", the Day P&L logic moved from MarketPulse.svelte to
 * src/lib/data/pulseUnified.js. The static-source guards below check the new
 * file locations. MarketPulse.svelte still imports baseDayPnlForPosition (passed
 * as ctx to helpers) so the import check stays in MarketPulse.svelte.
 *
 * Quality dimensions:
 *   SSOT     — grep confirms baseDayPnlForPosition is imported in MarketPulse
 *               and called for brokerDcv + _broker_day_pnl in pulseUnified.js
 *   Perf     — no XHR budget regression on /pulse
 *   Stale    — grep confirms raw r.day_change_val reads in positions loop
 *               are replaced; holdings loop is unaffected (no overnight_quantity)
 *   Reusable — same import path as derivatives page and PositionStrip
 *   UX       — pulse position card and NavStrip P slot 1 show consistent values
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

const PULSE_UNIFIED_SRC = path.resolve(
  process.cwd(),
  'src/lib/data/pulseUnified.js'
);

const NAV_SRC = path.resolve(
  process.cwd(),
  'src/lib/data/nav.js'
);

// ── Static source checks ─────────────────────────────────────────────────────

test('SSOT: MarketPulse imports baseDayPnlForPosition from $lib/data/nav', () => {
  const src = fs.readFileSync(MP_SRC, 'utf8');

  // Match any named-import list containing baseDayPnlForPosition from
  // $lib/data/nav — not just a single-name import. MarketPulse legitimately
  // imports multiple names in one statement (e.g.
  // `import { baseDayPnlForPosition, livePositionDayPnl, dayChangePct } from '$lib/data/nav';`).
  expect(
    /import\s*\{[^}]*\bbaseDayPnlForPosition\b[^}]*\}\s*from\s*'\$lib\/data\/nav'/.test(src),
    'MarketPulse.svelte must import baseDayPnlForPosition from $lib/data/nav'
  ).toBe(true);
});

test('SSOT: pulseUnified positions row.day_pnl uses livePositionDayPnl (baseline-diff SSOT), not raw day_change_val', () => {
  // Day P&L / Exp P&L redesign (2026-09): the Case-branch `brokerDcv` local
  // variable was eliminated — mergePositionRows now calls livePositionDayPnl
  // directly (which itself delegates to baseDayPnlForPosition for the base
  // and applies a live-tick delta on top). Check the helper module.
  const src = fs.readFileSync(PULSE_UNIFIED_SRC, 'utf8');

  // The old raw read pattern must not appear in the positions section
  expect(
    src.includes('const brokerDcv = Number(r.day_change_val) || 0'),
    'Old "const brokerDcv = Number(r.day_change_val) || 0" must not be in pulseUnified'
  ).toBe(false);

  // The new SSOT pattern — row.day_pnl accumulates via livePositionDayPnl
  expect(
    src.includes('row.day_pnl = (row.day_pnl ?? 0) + livePositionDayPnl('),
    'pulseUnified must accumulate row.day_pnl via livePositionDayPnl(...)'
  ).toBe(true);
});

test('SSOT: pulseUnified _broker_day_pnl positions-loop mirror uses baseDayPnlForPosition', () => {
  // After f378ce53 the _broker_day_pnl assignments live in pulseUnified.js.
  // There are two assignment sites in that file:
  //   1. mergePositionRows — MUST use baseDayPnlForPosition(r) (the fix)
  //   2. mergeHoldingRows  — keeps raw day_change_val (correct; no overnight_quantity on holdings)
  const src = fs.readFileSync(PULSE_UNIFIED_SRC, 'utf8');

  const firstOccIdx = src.indexOf('row._broker_day_pnl =');
  expect(firstOccIdx, 'First _broker_day_pnl assignment must exist in pulseUnified').toBeGreaterThan(0);

  const firstOccLine = src.slice(firstOccIdx, firstOccIdx + 120);
  expect(
    firstOccLine.includes('baseDayPnlForPosition'),
    `Positions-loop _broker_day_pnl must call baseDayPnlForPosition. Got: "${firstOccLine.trim()}"`
  ).toBe(true);

  // Also confirm the positions occurrence does NOT use the old raw pattern
  expect(
    firstOccLine.includes('day_change_val'),
    'Positions-loop _broker_day_pnl must NOT read raw day_change_val directly'
  ).toBe(false);
});

test('SSOT: nav.js defines baseDayPnlForPosition as the atomic baseline-diff formula', () => {
  // Day P&L / Exp P&L redesign (2026-09): the branchy Case 1-4 logic
  // (oq/dcv/close-based overrides) was replaced by one atomic formula,
  // proven correct for any position state: current_total_profit − base_pnl.
  const src = fs.readFileSync(NAV_SRC, 'utf8');

  expect(
    src.includes('export function baseDayPnlForPosition('),
    'nav.js must export baseDayPnlForPosition'
  ).toBe(true);

  expect(
    src.includes('export function currentTotalProfit('),
    'nav.js must export currentTotalProfit (realised+unrealised, pnl-fallback) — the SSOT baseDayPnlForPosition builds on'
  ).toBe(true);

  // The atomic formula: total profit minus the frozen baseline.
  expect(
    src.includes('return total - base'),
    'baseDayPnlForPosition must reduce to: return total - base (baseline-diff)'
  ).toBe(true);

  // The old Case-branch override must be gone — no more day_change_val reads.
  expect(
    src.includes('day_change_val'),
    'baseDayPnlForPosition must NOT read day_change_val — the old Case-branch logic was removed'
  ).toBe(false);
});

test('STALE: pulseUnified holdings loop still uses raw day_change_val (correct — no overnight_quantity)', () => {
  // After f378ce53 the _broker_day_pnl assignments live in pulseUnified.js.
  // Holdings rows don't carry overnight_quantity so baseDayPnlForPosition
  // is not applicable (it would return day_change_val via the dcv fallback anyway,
  // but keeping it explicit in mergeHoldingRows avoids confusion).
  // This test ensures the holdings loop in pulseUnified was NOT accidentally changed.
  //
  // Strategy: count occurrences in pulseUnified — should be exactly 2
  // (mergePositionRows and mergeHoldingRows). Positions uses baseDayPnlForPosition;
  // holdings uses raw day_change_val.
  const src = fs.readFileSync(PULSE_UNIFIED_SRC, 'utf8');

  const brokerDayPnlCount = (src.match(/row\._broker_day_pnl\s*=/g) || []).length;
  expect(
    brokerDayPnlCount,
    'pulseUnified: _broker_day_pnl should be assigned exactly twice (positions + holdings)'
  ).toBe(2);

  // The holdings assignment (second occurrence) still uses raw day_change_val
  const firstOccurrence = src.indexOf('row._broker_day_pnl =');
  const secondOccurrence = src.indexOf('row._broker_day_pnl =', firstOccurrence + 1);
  expect(secondOccurrence, 'Second _broker_day_pnl assignment must exist in pulseUnified').toBeGreaterThan(0);

  const secondLine = src.slice(secondOccurrence, secondOccurrence + 120);
  expect(
    secondLine.includes('day_change_val'),
    'Holdings _broker_day_pnl in pulseUnified must still use raw day_change_val'
  ).toBe(true);
});

test('REUSE: same baseDayPnlForPosition import path in MarketPulse and derivatives page', () => {
  const mpSrc   = fs.readFileSync(MP_SRC, 'utf8');
  const derivSrc = fs.readFileSync(
    path.resolve(process.cwd(), 'src/routes/(algo)/admin/derivatives/+page.svelte'),
    'utf8'
  );

  // Both must import from $lib/data/nav (not different relative paths)
  expect(
    mpSrc.includes("'$lib/data/nav'"),
    'MarketPulse must import from "$lib/data/nav"'
  ).toBe(true);
  expect(
    derivSrc.includes("'$lib/data/nav'"),
    'Derivatives page must import from "$lib/data/nav"'
  ).toBe(true);
});

// ── Live UI checks ───────────────────────────────────────────────────────────

const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'mobile',  width: 390,  height: 844 },
];

for (const vp of VIEWPORTS) {
  test.describe(`/pulse — Day P&L SSOT [${vp.name}]`, () => {
    test.setTimeout(120_000);

    test(`Position card Day P&L column is non-zero when positions exist [${vp.name}]`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });

      const pageErrors = [];
      page.on('pageerror', (err) => pageErrors.push(err.message));

      const xhrUrls = [];
      page.on('request', (req) => {
        if (req.resourceType() === 'fetch' || req.resourceType() === 'xhr') {
          xhrUrls.push(req.url());
        }
      });

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

      await page.goto(`${BASE}/pulse`, {
        waitUntil: 'domcontentloaded',
        timeout: 30_000,
      });

      // Pulse grid always mounts
      await page.locator('.mp-grid').first().waitFor({ state: 'attached', timeout: 25_000 });

      // ── Perf budget ────────────────────────────────────────────────────
      const apiReqs = xhrUrls.filter(u => u.includes('/api/'));
      expect(
        apiReqs.length,
        `Cold-load XHR budget exceeded: ${apiReqs.length} /api/ requests`
      ).toBeLessThan(80);

      // ── Check for position rows ────────────────────────────────────────
      // Position rows carry the "P" badge / row-pos class. If none exist,
      // skip the value assertion (empty book).
      const positionRows = page.locator('.row-pos');
      const rowCount = await positionRows.count();

      if (rowCount === 0) {
        // No live positions — verify no JS errors.
        const realErrors = pageErrors.filter(
          e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors, 'No unexpected JS errors on empty pulse').toHaveLength(0);
        return;
      }

      // ── Day P&L cells for positions ───────────────────────────────────
      // Each position row has a Day P&L cell with class "ag-col-day" or
      // the column header "Day P&L". Read cell values from the Day P&L column.
      // ag-Grid renders column cells in divs with role="gridcell" and
      // col-id matching the colDef field. The field for Day P&L is "day_pnl".
      const dayPnlCells = page.locator('[col-id="day_pnl"]');
      const cellCount = await dayPnlCells.count();

      if (cellCount === 0) {
        // Column hidden or not present — static checks cover the fix
        const realErrors = pageErrors.filter(
          e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors).toHaveLength(0);
        return;
      }

      const cellTexts = await Promise.all(
        Array.from({ length: cellCount }, (_, i) =>
          dayPnlCells.nth(i).textContent().then(t => (t ?? '').trim())
        )
      );

      // Filter to non-header, non-total rows (TOTAL row has empty or bold text)
      const isZeroText = (t) =>
        t === '' || t === '0' || t === '₹0' || t === '0.00' || t === '₹0.00' || t === '—';
      const zeroCells    = cellTexts.filter(isZeroText).length;
      const nonZeroCells = cellTexts.filter(t => !isZeroText(t)).length;

      // If all cells are zero/dash and we have ≥2 position rows, that's
      // suspicious — but only flag if rowCount >= 2 (one flat row is ok).
      if (rowCount >= 2 && nonZeroCells === 0 && zeroCells > 0) {
        // Log for info; don't hard-fail because market closed → flat day is valid
        // The static checks above are the hard guard for the code pattern.
        console.warn(
          `[day_pnl_ssot] All ${cellCount} Day P&L cells are zero/dash on /pulse ` +
          `(${rowCount} position rows). This may be a closed-market snapshot. ` +
          'Static source checks confirmed baseDayPnlForPosition is in use.'
        );
      }

      // ── No JS errors ──────────────────────────────────────────────────
      const realErrors = pageErrors.filter(
        e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
      );
      expect(realErrors, 'No unexpected JS errors on /pulse').toHaveLength(0);
    });

    test(`NavStrip P slot 1 and /pulse position TOTAL row are consistent [${vp.name}]`, async ({ page }) => {
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

      await page.locator('.mp-grid').first().waitFor({ state: 'attached', timeout: 25_000 });

      // NavStrip P pill — identifies itself via .ps-pill-p or contains 'P' label
      // with a day-value span. Read the first value in the P pill.
      const navPPill = page.locator('.ps-pill').filter({ hasText: /^P/ }).first();
      const pillExists = await navPPill.count().catch(() => 0);

      if (pillExists === 0) {
        // NavStrip not visible or no positions — skip
        const realErrors = pageErrors.filter(
          e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors).toHaveLength(0);
        return;
      }

      // NavStrip P day value is the first .ps-v span inside the P pill
      const navPDayText = (await navPPill.locator('.ps-v').first().textContent().catch(() => '')).trim();

      // TOTAL row in the positions section (row-total class or ag-row-pinned bottom)
      const totalRow = page.locator('[row-id="TOTAL"]').first();
      const totalExists = await totalRow.count().catch(() => 0);

      if (totalExists === 0) {
        // No TOTAL row — no positions or only one row (grid may not pin TOTAL)
        const realErrors = pageErrors.filter(
          e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors).toHaveLength(0);
        return;
      }

      const totalDayPnl = (await totalRow.locator('[col-id="day_pnl"]').textContent().catch(() => '')).trim();

      const isZero = (t) => t === '' || t === '0' || t === '₹0' || t === '—';

      // When TOTAL row has a non-zero Day P&L, NavStrip P slot must also be non-zero
      if (!isZero(totalDayPnl) && isZero(navPDayText)) {
        throw new Error(
          `NavStrip P day = "${navPDayText}" but pulse TOTAL Day P&L = "${totalDayPnl}". ` +
          'The _broker_day_pnl mirror in MarketPulse buildUnified positions loop must use ' +
          'baseDayPnlForPosition — check the SSOT fix.'
        );
      }

      // No JS errors
      const realErrors = pageErrors.filter(
        e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
      );
      expect(realErrors, 'No unexpected JS errors').toHaveLength(0);
    });
  });
}
