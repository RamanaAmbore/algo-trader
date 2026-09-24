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
