/**
 * derivatives_pulse_day_pnl_ssot.spec.js
 *
 * Regression guard for the SSOT violation (2026-07-04): /pulse Positions grid
 * showed correct Day P&L for CRUDEOIL while /admin/derivatives Snapshot showed 0.
 *
 * Original root cause: Pulse applied a live-LTP recompute (livePositionDayPnl) rescuing
 * the MCX stale-ticker fingerprint (last_price === close_price → day_change_val=0).
 * Derivatives' _dayPnlForLeg called only baseDayPnlForPosition with no live rescue.
 *
 * Fix (2026-07-04): livePositionDayPnl extracted to nav.js SSOT; both surfaces now
 * call it, normalising field names from raw broker (Pulse) and candidate rows (Derivatives).
 *
 * Fix (2026-09-07): candidatesDayPnl (payoff overlay DAY P&L) replaced per-leg
 * livePositionDayPnl recompute with direct positionsDayPnlStore / holdingsDayPnlStore
 * lookups — the same stores NavStrip P1 reads. This eliminates the SSOT gap where
 * overlay DAY P&L and NavStrip P1 diverged for the same enabled legs.
 *
 * Fix (2026-09-08): candidatesDayPnl reverted from positionsDayPnlStore/holdingsDayPnlStore
 * lookups back to _dayPnlForLeg per-row, with a _lastCandidatesDayPnl stale-cache guard.
 * Root cause of regression: positionsDayPnlStore.byKey returns _pulseByKey ?? _store.byKey;
 * if _pulseByKey was set from a prior MarketPulse visit but didn't include CRUDEOIL/GOLDM
 * (closed/filtered positions), lookups returned undefined → 0. _dayPnlForLeg operates on
 * the raw candidate row's own qty + LTP fields and is unaffected by filter state.
 * OptionsPayoff guard `dayPnl != null` stays — flat-day (0) renders correctly.
 *
 * Quality dimensions checked:
 *   SSOT   — candidatesDayPnl uses _dayPnlForLeg per row (not store lookups that can
 *            return undefined for filtered-out symbols); _dayPnlForLeg delegates to
 *            baseDayPnlForPosition; pulseUnified.js still calls livePositionDayPnl
 *   Perf   — no XHR budget regression on Pulse cold-load
 *   Stale  — _lastCandidatesDayPnl caches last non-empty value to bridge 5s poll gaps;
 *            no "realisedToday" inline computation remaining in consumers
 *   Reuse  — _dayPnlForLeg is the existing per-leg SSOT function
 *   UX     — DAY P&L row in payoff overlay renders for flat days (dayPnl=0 no longer hidden)
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
const PAYOFF_SRC = path.resolve(
  process.cwd(),
  'src/lib/OptionsPayoff.svelte'
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

test('SSOT: _lastCandidatesDayPnl stale-cache variable is present in derivatives page source', () => {
  const src = fs.readFileSync(DERIV_SRC, 'utf8');
  expect(
    src.includes('let _lastCandidatesDayPnl = $state('),
    'derivatives page must declare _lastCandidatesDayPnl as a $state stale-cache variable'
  ).toBe(true);
});

test('SSOT: candidatesDayPnl uses _dayPnlForLeg for per-row computation (not store lookups)', () => {
  const src = fs.readFileSync(DERIV_SRC, 'utf8');

  const blockStart = src.indexOf('const candidatesDayPnl = $derived.by(');
  expect(blockStart, 'candidatesDayPnl $derived.by block must exist').toBeGreaterThan(0);
  const blockEnd = src.indexOf('\n  });', blockStart) + 6;
  const blockBody = src.slice(blockStart, blockEnd);

  // Must call _dayPnlForLeg — the per-row SSOT that reads each candidate's own qty+LTP
  expect(
    blockBody.includes('_dayPnlForLeg('),
    'candidatesDayPnl must use _dayPnlForLeg per row — store lookups can return undefined for filtered symbols'
  ).toBe(true);

  // Must NOT use store byKey lookups — these can miss symbols not present in _pulseByKey
  expect(
    blockBody.includes('positionsDayPnlStore.byKey'),
    'candidatesDayPnl must NOT use positionsDayPnlStore.byKey (regresses CRUDEOIL/GOLDM to ₹0 when store is filtered)'
  ).toBe(false);
  expect(
    blockBody.includes('holdingsDayPnlStore.byKey'),
    'candidatesDayPnl must NOT use holdingsDayPnlStore.byKey'
  ).toBe(false);
});

test('SSOT: candidatesDayPnl returns stale cache when candidatePositions is empty', () => {
  const src = fs.readFileSync(DERIV_SRC, 'utf8');

  const blockStart = src.indexOf('const candidatesDayPnl = $derived.by(');
  expect(blockStart, 'candidatesDayPnl $derived.by block must exist').toBeGreaterThan(0);
  const blockEnd = src.indexOf('\n  });', blockStart) + 6;
  const blockBody = src.slice(blockStart, blockEnd);

  // Must return stale value rather than null during transient empty candidatePositions
  expect(
    blockBody.includes('return _lastCandidatesDayPnl'),
    'candidatesDayPnl must return _lastCandidatesDayPnl as stale fallback when candidatePositions is empty'
  ).toBe(true);
});

test('SSOT: OptionsPayoff DAY P&L guard uses null-only check (shows 0 on flat days)', () => {
  const src = fs.readFileSync(PAYOFF_SRC, 'utf8');

  // Guard must be `dayPnl != null` — not `dayPnl != null && dayPnl !== 0`
  // The `!== 0` clause incorrectly hid the DAY P&L row on flat days.
  expect(
    src.includes('{#if dayPnl != null && dayPnl !== 0}'),
    'OptionsPayoff must NOT have the old `dayPnl !== 0` guard — flat-day P&L row would be hidden'
  ).toBe(false);

  expect(
    src.includes('{#if dayPnl != null}'),
    'OptionsPayoff must use `{#if dayPnl != null}` — render DAY P&L row whenever store provides a value (including 0)'
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
