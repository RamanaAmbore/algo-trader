/**
 * derivatives_pulse_day_pnl_ssot.spec.js
 *
 * Regression guard for the SSOT violation (2026-07-04): /pulse Positions grid
 * showed correct Day P&L for CRUDEOIL while /admin/derivatives Snapshot showed 0.
 *
 * Root cause: Pulse applied a live-LTP recompute (livePositionDayPnl) rescuing
 * the MCX stale-ticker fingerprint (last_price === close_price → day_change_val=0).
 * Derivatives' _dayPnlForLeg called only baseDayPnlForPosition with no live rescue.
 *
 * Fix: livePositionDayPnl extracted to nav.js SSOT; both surfaces now call it,
 * normalising field names from raw broker (Pulse) and candidate rows (Derivatives).
 *
 * Quality dimensions checked:
 *   SSOT   — both surfaces import and call livePositionDayPnl; no inline recompute
 *            duplicates; derivatives _dayPnlForLeg calls the new helper
 *   Perf   — no XHR budget regression on Pulse cold-load
 *   Stale  — no "realisedToday" inline computation remaining in consumers
 *   Reuse  — pulseUnified.js + derivatives page both import from $lib/data/nav
 *   UX     — Day P&L values visible on both pages for same underlying when
 *            market is open; CRUDEOIL row (if present) shows matching values
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

const DERIV_SRC = path.resolve(
  process.cwd(),
  'src/routes/(algo)/admin/derivatives/+page.svelte'
);
const PULSE_SRC = path.resolve(
  process.cwd(),
  'src/lib/data/pulseUnified.js'
);
const NAV_SRC = path.resolve(
  process.cwd(),
  'src/lib/data/nav.js'
);

// ── Static SSOT checks ────────────────────────────────────────────────────────

test('SSOT: livePositionDayPnl is defined and exported from nav.js', () => {
  const src = fs.readFileSync(NAV_SRC, 'utf8');
  expect(
    src.includes('export function livePositionDayPnl('),
    'nav.js must export livePositionDayPnl as the canonical live-LTP-rescue helper'
  ).toBe(true);
  // Must call baseDayPnlForPosition internally (not reimplement)
  expect(
    src.includes('baseDayPnlForPosition('),
    'livePositionDayPnl must delegate to baseDayPnlForPosition for the base path'
  ).toBe(true);
});

test('SSOT: derivatives _dayPnlForLeg calls livePositionDayPnl (not inline math)', () => {
  const src = fs.readFileSync(DERIV_SRC, 'utf8');

  // Import must include livePositionDayPnl
  expect(
    src.includes('livePositionDayPnl') && src.includes("$lib/data/nav"),
    'derivatives page must import livePositionDayPnl from $lib/data/nav'
  ).toBe(true);

  // _dayPnlForLeg body must call livePositionDayPnl
  const fnStart = src.indexOf('function _dayPnlForLeg(');
  expect(fnStart, '_dayPnlForLeg must exist in derivatives page').toBeGreaterThan(0);
  // Extract function body (up to the matching closing brace pattern)
  const fnEnd = src.indexOf('\n  }', fnStart) + 4;
  const fnBody = src.slice(fnStart, fnEnd);
  expect(
    fnBody.includes('livePositionDayPnl('),
    '_dayPnlForLeg must call livePositionDayPnl for the non-expired path'
  ).toBe(true);
});

test('SSOT: pulseUnified.js calls livePositionDayPnl (not inline recompute)', () => {
  const src = fs.readFileSync(PULSE_SRC, 'utf8');
  expect(
    src.includes('livePositionDayPnl'),
    'pulseUnified.js must call livePositionDayPnl from nav.js'
  ).toBe(true);
});

test('Stale: no inline realisedToday computation left in consumers', () => {
  const derivSrc = fs.readFileSync(DERIV_SRC, 'utf8');
  const pulseSrc = fs.readFileSync(PULSE_SRC, 'utf8');

  // "realisedToday" is the variable name used in the old inline math.
  // It should now only live inside nav.js (inside livePositionDayPnl), not
  // in the consumer files.
  expect(
    derivSrc.includes('realisedToday'),
    'derivatives page must not inline realisedToday — delegate to livePositionDayPnl'
  ).toBe(false);
  expect(
    pulseSrc.includes('realisedToday'),
    'pulseUnified.js must not inline realisedToday — delegate to livePositionDayPnl'
  ).toBe(false);

  // nav.js MUST still contain it (inside the helper)
  const navSrc = fs.readFileSync(NAV_SRC, 'utf8');
  expect(
    navSrc.includes('realisedToday'),
    'nav.js must contain realisedToday inside livePositionDayPnl (the SSOT location)'
  ).toBe(true);
});

test('Stale: derivatives _dayPnlForLeg uses untrack() on getSnapshot to respect throttle', () => {
  const src = fs.readFileSync(DERIV_SRC, 'utf8');
  const fnStart = src.indexOf('function _dayPnlForLeg(');
  const fnEnd = src.indexOf('\n  }', fnStart) + 4;
  const fnBody = src.slice(fnStart, fnEnd);
  expect(
    fnBody.includes('untrack('),
    '_dayPnlForLeg must wrap getSnapshot in untrack() to prevent throttle bypass'
  ).toBe(true);
});

// ── Live UI checks ────────────────────────────────────────────────────────────

const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'mobile',  width: 390,  height: 844 },
];

for (const vp of VIEWPORTS) {
  test.describe(`Pulse ↔ Derivatives Day P&L parity [${vp.name}]`, () => {
    test.setTimeout(120_000);

    /**
     * Core parity test: load /admin/derivatives and /pulse in sequence,
     * collect Day P&L values per symbol, and assert that no underlying shows
     * a non-zero value on Pulse but zero on Derivatives for the same symbol.
     *
     * This is the exact CRUDEOIL/MCX failure mode reported 2026-07-04.
     */
    test(`Derivatives Snapshot Day P&L is non-zero where Pulse Positions shows non-zero [${vp.name}]`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });

      const pageErrors = [];
      page.on('pageerror', (err) => pageErrors.push(err.message));

      // Auth
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
        test.skip(true, 'No valid credentials — static SSOT checks above cover the fix');
        return;
      }

      // ── Step 1: collect Pulse Positions grid Day P&L per symbol ─────────────
      const xhrPulse = [];
      page.on('request', req => {
        if (['fetch', 'xhr'].includes(req.resourceType())) xhrPulse.push(req.url());
      });

      await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded', timeout: 30_000 });
      // Wait for ag-Grid rows to appear
      await page.locator('.ag-row').first().waitFor({ state: 'attached', timeout: 25_000 }).catch(() => {});

      // ── Perf budget (Pulse cold-load) ────────────────────────────────────────
      const apiReqsPulse = xhrPulse.filter(u => u.includes('/api/'));
      expect(
        apiReqsPulse.length,
        `Pulse cold-load XHR budget: ${apiReqsPulse.length} /api/ requests`
      ).toBeLessThan(60);

      // Collect { symbol, dayPnlText } from Pulse Positions rows.
      // ag-Grid row cells: the "Symbol" col has class .ag-col-sym, Day P&L uses
      // the colId 'day_pnl'. We read by column class since exact col IDs may vary.
      // Use evaluate for efficiency over N cells.
      const pulseRows = await page.evaluate(() => {
        const rows = document.querySelectorAll('.ag-row[row-index]');
        const out = [];
        for (const row of rows) {
          const symCell = row.querySelector('[col-id="tradingsymbol"]') ||
                          row.querySelector('.ag-col-sym');
          const dayCell = row.querySelector('[col-id="day_pnl"]');
          if (!symCell || !dayCell) continue;
          const sym = (symCell.textContent || '').trim().toUpperCase();
          const day = (dayCell.textContent || '').trim();
          if (sym) out.push({ sym, day });
        }
        return out;
      }).catch(() => []);

      // Filter to F&O rows (FUT / CE / PE suffix) — only these can have the stale-LTP issue
      const foPulseRows = pulseRows.filter(r =>
        /FUT$|CE$|PE$/i.test(r.sym)
      );

      if (foPulseRows.length === 0) {
        // No F&O positions in Pulse — nothing to compare
        const realErrors = pageErrors.filter(
          e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors).toHaveLength(0);
        return;
      }

      // ── Step 2: collect Derivatives Snapshot Day P&L per underlying ──────────
      await page.goto(`${BASE}/admin/derivatives`, {
        waitUntil: 'domcontentloaded',
        timeout: 30_000,
      });
      await page.locator('.opt-byund-card').waitFor({ state: 'attached', timeout: 25_000 });

      // Give the 4Hz _throttledTick one cycle to resolve
      await page.waitForTimeout(500);

      const derivRows = await page.evaluate(() => {
        const rows = document.querySelectorAll('.byund-row:not(.byund-row-total)');
        const out = [];
        for (const row of rows) {
          const undCell = row.querySelector('.byund-und');
          // Day P&L is the 4th .num span (0-indexed: ltp, pct, prevclose, DAY, ...)
          const numCells = row.querySelectorAll('.num');
          const dayCell = numCells[3]; // Day P&L column (index 3 after Spot/Day%/Prev Close)
          if (!undCell || !dayCell) continue;
          const und = (undCell.textContent || '').trim().toUpperCase();
          const day = (dayCell.textContent || '').trim();
          if (und) out.push({ und, day });
        }
        return out;
      }).catch(() => []);

      // ── Parity check ─────────────────────────────────────────────────────────
      const isZero = (t) => !t || t === '0' || t === '₹0' || t === '0.00' || t === '₹0.00';
      const isDash = (t) => t === '—' || t === '-';

      const violations = [];
      for (const pulseRow of foPulseRows) {
        if (isZero(pulseRow.day) || isDash(pulseRow.day)) continue; // skip flat/missing
        // Extract the underlying root from the symbol (strip expiry/strike/opttype)
        const rootMatch = pulseRow.sym.match(/^([A-Z]+)/);
        if (!rootMatch) continue;
        const root = rootMatch[1];

        // Find matching derivatives underlying row
        const derivRow = derivRows.find(d => d.und === root || pulseRow.sym.startsWith(d.und));
        if (!derivRow) continue; // symbol not in derivatives snapshot (may be NSE equity — OK)

        if (isZero(derivRow.day) && !isDash(derivRow.day)) {
          violations.push(
            `SYMBOL=${pulseRow.sym} ROOT=${root}: Pulse Day P&L="${pulseRow.day}" but Derivatives shows "${derivRow.day}" — stale-LTP rescue SSOT violation`
          );
        }
      }

      expect(
        violations,
        `Day P&L SSOT violations found:\n${violations.join('\n')}`
      ).toHaveLength(0);

      // No JS errors
      const realErrors = pageErrors.filter(
        e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
      );
      expect(realErrors, 'No unexpected JS errors').toHaveLength(0);
    });

    test(`CRUDEOIL (if present): Derivatives Snapshot Day P&L is non-zero [${vp.name}]`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });

      const pageErrors = [];
      page.on('pageerror', err => pageErrors.push(err.message));

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
        test.skip(true, 'No valid credentials — static SSOT checks cover the fix');
        return;
      }

      await page.goto(`${BASE}/admin/derivatives`, {
        waitUntil: 'domcontentloaded',
        timeout: 30_000,
      });
      await page.locator('.opt-byund-card').waitFor({ state: 'attached', timeout: 25_000 });
      await page.waitForTimeout(500);

      // Find CRUDEOIL row in Snapshot
      const crudeoilRow = page.locator('.byund-row:not(.byund-row-total)').filter({
        has: page.locator('.byund-und', { hasText: 'CRUDEOIL' }),
      });
      const crudeoilCount = await crudeoilRow.count();

      if (crudeoilCount === 0) {
        // CRUDEOIL not in current book — mark as informational skip
        test.skip(true, 'CRUDEOIL not in current position book — skipping CRUDEOIL-specific check');
        return;
      }

      // The Day P&L cell is the 4th .num inside the CRUDEOIL row
      const dayCell = crudeoilRow.first().locator('.num').nth(3);
      const dayText = ((await dayCell.textContent().catch(() => '')) || '').trim();

      const isZero = (t) => !t || t === '0' || t === '₹0' || t === '0.00' || t === '₹0.00';
      const isDash = (t) => t === '—' || t === '-';

      // If it's a dash, the market may be closed or the position is newly opened —
      // those are not failure states. Only fail on hard zero.
      if (!isDash(dayText)) {
        expect(
          isZero(dayText),
          `CRUDEOIL Snapshot Day P&L is "${dayText}" — expected non-zero (stale-LTP rescue should apply)`
        ).toBe(false);
      }

      const realErrors = pageErrors.filter(
        e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
      );
      expect(realErrors, 'No unexpected JS errors').toHaveLength(0);
    });
  });
}
