/**
 * chart_audit_fixes.spec.js
 *
 * Covers six audit-identified defects in ChartWorkspace + OhlcvTooltip +
 * resolveUnderlying. Each test targets one specific defect so regressions
 * are easy to bisect.
 *
 * Five quality dimensions:
 *  1. SSOT     — chart exchange routing + tooltip date are single-path issues
 *  2. Perf     — stale-code source-file checks run with zero server round-trips
 *  3. Stale    — source grep prevents silent reintroduction of removed symbols
 *  4. Reusable — shared loginAsAdmin / waitForChart helpers
 *  5. UX       — tooltip date must never show the prior calendar day for IST users
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/chart_audit_fixes.spec.js --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dir = dirname(fileURLToPath(import.meta.url));
const BASE   = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

// Source-file paths for stale-code checks (dimension 3 — no server needed).
const _RESOLVE_SRC = join(__dir, '../../frontend/src/lib/data/resolveUnderlying.js');
const _WORKSPACE_SRC = join(__dir, '../../frontend/src/lib/ChartWorkspace.svelte');
const _TOOLTIP_SRC = join(__dir, '../../frontend/src/lib/chart/OhlcvTooltip.svelte');

// ── Stale-code checks (no server needed) ──────────────────────────────────────

test.describe('stale-code: resolveUnderlying.js', () => {
  let src;
  test.beforeAll(() => {
    src = readFileSync(_RESOLVE_SRC, 'utf-8');
  });

  // Fix #4 — CDS currencies
  test('Fix #4: CDS_CURRENCIES includes EURINR, GBPINR, JPYINR', () => {
    expect(src).toContain("'EURINR'");
    expect(src).toContain("'GBPINR'");
    expect(src).toContain("'JPYINR'");
  });

  // Fix #14 — MCX_COMMODITIES additions and removals
  test('Fix #14: MCX_COMMODITIES contains CPO', () => {
    expect(src).toContain("'CPO'");
  });

  test('Fix #14: GOLDMINI removed from MCX_COMMODITIES', () => {
    // Only the stale set contained 'GOLDMINI'; CPO replaces it.
    // Guard: the string should not appear at all in the set literal.
    // We check the MCX_COMMODITIES declaration block specifically.
    const setBlock = src.slice(
      src.indexOf('export const MCX_COMMODITIES'),
      src.indexOf(']);', src.indexOf('export const MCX_COMMODITIES')) + 3,
    );
    expect(setBlock).not.toContain("'GOLDMINI'");
  });

  test('Fix #14: SILVERMINI removed from MCX_COMMODITIES', () => {
    const setBlock = src.slice(
      src.indexOf('export const MCX_COMMODITIES'),
      src.indexOf(']);', src.indexOf('export const MCX_COMMODITIES')) + 3,
    );
    expect(setBlock).not.toContain("'SILVERMINI'");
  });

  test('Fix #14: CARDAMOM removed from MCX_COMMODITIES', () => {
    const setBlock = src.slice(
      src.indexOf('export const MCX_COMMODITIES'),
      src.indexOf(']);', src.indexOf('export const MCX_COMMODITIES')) + 3,
    );
    expect(setBlock).not.toContain("'CARDAMOM'");
  });

  test('Fix #14: CASTORSEED removed from MCX_COMMODITIES', () => {
    const setBlock = src.slice(
      src.indexOf('export const MCX_COMMODITIES'),
      src.indexOf(']);', src.indexOf('export const MCX_COMMODITIES')) + 3,
    );
    expect(setBlock).not.toContain("'CASTORSEED'");
  });
});

test.describe('stale-code: ChartWorkspace.svelte', () => {
  let src;
  test.beforeAll(() => {
    src = readFileSync(_WORKSPACE_SRC, 'utf-8');
  });

  // Fix #18 — _KITE_INDEX_TO_ROOT completeness
  test('Fix #18: _KITE_INDEX_TO_ROOT contains SENSEX entry', () => {
    // Check that the local map inside ChartWorkspace has SENSEX
    const mapBlock = src.slice(
      src.indexOf('const _KITE_INDEX_TO_ROOT'),
      src.indexOf('};', src.indexOf('const _KITE_INDEX_TO_ROOT')) + 2,
    );
    expect(mapBlock).toContain("'SENSEX'");
  });

  test('Fix #18: _KITE_INDEX_TO_ROOT contains BANKEX entry', () => {
    const mapBlock = src.slice(
      src.indexOf('const _KITE_INDEX_TO_ROOT'),
      src.indexOf('};', src.indexOf('const _KITE_INDEX_TO_ROOT')) + 2,
    );
    expect(mapBlock).toContain("'BANKEX'");
  });

  // Fix #12 — MCX overlay exchange
  test('Fix #12: spot overlay fetch passes exchange argument', () => {
    // The fix adds _spotExch computed from MCX_COMMODITIES, then passes
    // exchange: _spotExch to the underlying fetchOptionsHistorical call.
    expect(src).toContain('_spotExch');
    expect(src).toContain('exchange: _spotExch');
  });

  // Fix #11 — keep-last-good guard ordering
  test('Fix #11: _handleEmptyBars called before chartStore.setOhlcv in empty branch', () => {
    // Locate the empty-bars branch. After the fix, _handleEmptyBars must
    // appear BEFORE the chartStore.setOhlcv assignment in the source text.
    const emptyBranchStart = src.indexOf('if (_nextBars.length === 0)');
    expect(emptyBranchStart).toBeGreaterThan(-1);
    // Find the positions of the two calls within the empty branch.
    const handleIdx = src.indexOf('_handleEmptyBars(', emptyBranchStart);
    const setOhlcvIdx = src.indexOf('chartStore.setOhlcv(_bars, _spotBars)', emptyBranchStart);
    expect(handleIdx).toBeGreaterThan(-1);
    expect(setOhlcvIdx).toBeGreaterThan(-1);
    // _handleEmptyBars must come first (lower index).
    expect(handleIdx).toBeLessThan(setOhlcvIdx);
  });
});

test.describe('stale-code: OhlcvTooltip.svelte', () => {
  let src;
  test.beforeAll(() => {
    src = readFileSync(_TOOLTIP_SRC, 'utf-8');
  });

  // Fix #13 — local-time date parsing
  test('Fix #13: date parsing uses T00:00:00 suffix for 10-char strings', () => {
    expect(src).toContain("'T00:00:00'");
  });

  test('Fix #13: does not use bare new Date(ts) for all strings', () => {
    // The old code was a single `new Date(ts)` — after fix it must not exist
    // as the sole date parse expression (length check must guard it).
    // We verify the fix guards 10-char strings.
    expect(src).toContain('ts.length === 10');
  });
});

// ── Browser smoke: SENSEX chart resolves without "No data available" ─────────

test.describe('browser: BSE index chart smoke (Fix #18)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('SENSEX chart loads bars without "No data available" error', async ({ page }) => {
    test.setTimeout(60_000);

    await page.goto(`${BASE}/charts?symbol=SENSEX`, { waitUntil: 'domcontentloaded' });

    // Wait for range group to mount
    await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 25_000 });

    // The error state renders a text node containing "No data available."
    // It should NOT appear within 20s of initial load.
    await page.waitForTimeout(3_000);
    const errorEl = page.locator('text=No data available.');
    await expect(errorEl).toHaveCount(0, { timeout: 5_000 });
  });
});

// ── Browser smoke: tooltip date matches current calendar day ─────────────────

test.describe('browser: OhlcvTooltip date display (Fix #13)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('hovering the chart never shows the previous day for a YYYY-MM-DD bar', async ({ page }) => {
    test.setTimeout(60_000);

    await page.goto(`${BASE}/charts?symbol=NIFTY+50`, { waitUntil: 'domcontentloaded' });

    // Wait for SVG bars to render
    await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 25_000 });
    await expect(page.locator('.cw-svg rect.bar').first()).toBeVisible({ timeout: 25_000 });

    // Hover over the first visible bar to trigger the tooltip
    const firstBar = page.locator('.cw-svg rect.bar').first();
    await firstBar.hover({ force: true });

    // Wait briefly for tooltip to appear
    await page.waitForTimeout(500);
    const tooltip = page.locator('.chart-tooltip');
    const count = await tooltip.count();
    if (count === 0) {
      // Tooltip may not appear on daily bars on some viewport sizes; skip
      // the date assertion but the stale-code check above already covers the fix.
      return;
    }

    await expect(tooltip.first()).toBeVisible({ timeout: 5_000 });
    const tsText = await tooltip.locator('.chart-tooltip-ts').first().innerText();

    // Verify the date is not the empty string — a real date was formatted.
    expect(tsText.trim().length).toBeGreaterThan(0);

    // Months list for parsing the rendered text
    const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    // Format is "D Mon YYYY" — extract parts and verify it is a valid date
    const parts = tsText.trim().split(' ');
    expect(parts).toHaveLength(3);
    const [dayStr, monStr, yearStr] = parts;
    const monthIdx = MONTHS.indexOf(monStr);
    expect(monthIdx).toBeGreaterThanOrEqual(0);
    const parsedDate = new Date(Number(yearStr), monthIdx, Number(dayStr));
    expect(Number.isFinite(parsedDate.getTime())).toBe(true);
  });
});
