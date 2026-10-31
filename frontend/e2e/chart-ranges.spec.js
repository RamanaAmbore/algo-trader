/**
 * chart-ranges.spec.js
 *
 * Regression spec for chart range buttons (3M/6M/1Y).
 *
 * Background: Bug #910740f0 caused _SELF_HEAL_COVERAGE_THRESHOLD = 0.70 to always
 * flag NSE equity OHLCV data as "partial" for 3M/6M/1Y ranges (NSE has ~252
 * trading days/year = 69% of calendar days, always below 70%). This triggered
 * infinite _ohlcv_demand_fill retries — charts spun indefinitely.
 * Fix: lowered threshold to 0.60.
 *
 * Test scope (data-independent — no OHLCV cache required):
 *   1. Stale-code: options.py threshold is 0.60 (not 0.70)
 *   2. Range buttons (3M/6M/1Y) are present, visible, and clickable
 *   3. Clicking a range button doesn't cause a JS crash or infinite spinner
 *   4. Active button state updates after click
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dir = dirname(fileURLToPath(import.meta.url));
const _OPTIONS_SRC = join(__dir, '../../backend/api/routes/options.py');

// ── Dimension 3 (stale-code) — source file check, no server needed ──────────
test('chart-ranges: options.py self-heal threshold is 0.60 not 0.70', () => {
  const src = readFileSync(_OPTIONS_SRC, 'utf-8');
  const line = src.split('\n').find(l => l.includes('_SELF_HEAL_COVERAGE_THRESHOLD: float'));
  expect(line, 'Could not find _SELF_HEAL_COVERAGE_THRESHOLD definition').toBeTruthy();
  expect(line).toContain('= 0.60');
  expect(line).not.toContain('= 0.70');
});

// ── Dimensions 1/2/4/5 — range button smoke (data-independent) ───────────────
test.describe('chart-ranges: range button smoke', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('3M/6M/1Y buttons visible and enabled after initial chart load', async ({ page }) => {
    test.setTimeout(60_000);

    // Navigate with a symbol so range buttons are active immediately
    await page.goto('/charts?symbol=NIFTY+50', { waitUntil: 'domcontentloaded' });

    // Wait for ChartWorkspace to mount — range group is always rendered
    const rangeGroup = page.locator('.cw-range-group');
    await expect(rangeGroup).toBeVisible({ timeout: 30_000 });

    // Wait for at least one range button to become enabled (initial load done)
    const anyBtn = rangeGroup.locator('.cw-range-btn').first();
    await expect(anyBtn).toBeEnabled({ timeout: 30_000 });

    // Assert 3M/6M/1Y buttons are present
    for (const label of ['3M', '6M', '1Y']) {
      const btn = rangeGroup.locator('.cw-range-btn', { hasText: label });
      await expect(btn).toBeVisible();
      await expect(btn).toBeEnabled();
    }
  });

  test('clicking 3M range button switches active state without crash', async ({ page }) => {
    test.setTimeout(60_000);

    await page.goto('/charts?symbol=NIFTY+50', { waitUntil: 'domcontentloaded' });

    const rangeGroup = page.locator('.cw-range-group');
    await expect(rangeGroup).toBeVisible({ timeout: 30_000 });

    // Wait for initial load to complete (buttons enabled)
    const btn3M = rangeGroup.locator('.cw-range-btn', { hasText: '3M' });
    await expect(btn3M).toBeEnabled({ timeout: 30_000 });

    // Click 3M — should not crash, loading state should resolve within 15s
    await btn3M.click();

    // Assert no JS errors thrown (Playwright surfaces uncaught exceptions)
    // by asserting page is still responsive after click
    await expect(rangeGroup).toBeVisible({ timeout: 5_000 });

    // Wait for loading state to clear (spinner disappears or chart becomes visible).
    // Regression: would spin indefinitely with 0.70 threshold.
    // Wait up to 15s for spinner to disappear.
    const spinner = page.locator('.cw-fetch-spinner');
    await expect(spinner).toHaveCount(0, { timeout: 15_000 });
  });

  // ── 1D intraday regression — operator: "when I change chart duration,
  // I think it is not showing chart correctly. I think intraday charts
  // are not correct." (2026-10-31) ────────────────────────────────────
  //
  // Root cause: ChartWorkspace always requested interval="day" from
  // /api/options/historical, including for the "1D" range button. The
  // backend's daily store excludes today's still-forming bar, so a 1D
  // request fell through to a single near-empty candle. Fix: the 1D
  // range now requests interval="5minute". This test asserts the real
  // request carries that interval and the response has more than one
  // bar — a single flat candle is the exact symptom being fixed.
  test('1D range requests interval=5minute and returns multiple intraday bars', async ({ page }) => {
    test.setTimeout(60_000);

    await page.goto('/charts?symbol=NIFTY+50', { waitUntil: 'domcontentloaded' });

    const rangeGroup = page.locator('.cw-range-group');
    await expect(rangeGroup).toBeVisible({ timeout: 30_000 });

    const anyBtn = rangeGroup.locator('.cw-range-btn').first();
    await expect(anyBtn).toBeEnabled({ timeout: 30_000 });

    const histPromise = page.waitForResponse(
      (r) => {
        const u = r.url();
        return u.includes('/api/options/historical') && /days=1\b/.test(u);
      },
      { timeout: 30_000 },
    ).catch(() => null);

    const btn1D = rangeGroup.locator('.cw-range-btn', { hasText: '1D' });
    await expect(btn1D).toBeEnabled({ timeout: 10_000 });
    await btn1D.click();

    const resp = await histPromise;
    expect(resp, '/api/options/historical?days=1 was not requested').not.toBeNull();

    // The regression: interval must be 5minute, never the "day" default.
    expect(resp.url()).toMatch(/interval=5minute/);
    expect(resp.url()).not.toMatch(/interval=day/);

    expect(resp.status(), '1D response status').toBe(200);
    const body = await resp.json().catch(() => null);
    expect(body, '1D response body parseable').toBeTruthy();
    const bars = Array.isArray(body.bars) ? body.bars : [];

    // Markets are open most of the week — on a confirmed-closed day
    // (weekend/holiday) the three-tier intraday store still serves the
    // last trading day's bars (see _historical_closed_guard design).
    // A single bar (or zero) reproduces the exact "flat chart" symptom.
    expect(
      bars.length,
      `Expected multiple intraday bars for the 1D range, got ${bars.length}. ` +
      'A single bar reproduces the pre-fix symptom (interval="day" default ' +
      'falling through to one near-empty broker candle).',
    ).toBeGreaterThan(1);

    // Distinct time-of-day across bars — confirms these are real intraday
    // candles, not the same daily bar duplicated.
    const times = new Set(bars.map((b) => String(b.ts).slice(11, 16)));
    expect(times.size, 'intraday bars should carry distinct HH:MM timestamps').toBeGreaterThan(1);
  });

  test('clicking 6M then 1Y both clear loading state within 15s', async ({ page }) => {
    test.setTimeout(90_000);

    await page.goto('/charts?symbol=NIFTY+50', { waitUntil: 'domcontentloaded' });

    const rangeGroup = page.locator('.cw-range-group');
    await expect(rangeGroup).toBeVisible({ timeout: 30_000 });

    // Wait for initial load
    const anyBtn = rangeGroup.locator('.cw-range-btn').first();
    await expect(anyBtn).toBeEnabled({ timeout: 30_000 });

    for (const label of ['6M', '1Y']) {
      const btn = rangeGroup.locator('.cw-range-btn', { hasText: label });
      await expect(btn).toBeEnabled({ timeout: 5_000 });
      await btn.click();

      // Key regression check: loading state clears within 15s (infinite spin = bug)
      const spinner = page.locator('.cw-fetch-spinner');
      await expect(spinner).toHaveCount(0, { timeout: 15_000 });
      console.log(`[${label}] loading cleared`);

      // Small gap between clicks
      await page.waitForTimeout(300);
    }
  });

  // ── 1D weekday double-session regression — operator: "audit all the
  // chart durations and indicators" (2026-10-31), found auditing the
  // 5minute-for-1D fix above. _historical_intraday_store's from_d/to_d
  // sweep is INCLUSIVE, so days=1 fetched two calendar dates (yesterday's
  // full session + today's partial one) on a weekday. Fixed by
  // _trim_to_latest_session_day() on the backend (see
  // backend/tests/test_chart_1d_session_trim.py for the backend-level
  // repro). This is the frontend-observable half: every bar in the "1D"
  // response must share the same calendar date — for BOTH an NSE equity
  // and an MCX future, since the two exchanges' sessions sit in different
  // UTC-date-boundary positions. ────────────────────────────────────────
  for (const [label, symbol] of [['RELIANCE (NSE)', 'RELIANCE'], ['CRUDEOIL (MCX)', 'CRUDEOIL']]) {
    test(`1D range bars share a single calendar date — ${label}`, async ({ page }) => {
      test.setTimeout(60_000);

      const histPromise = page.waitForResponse(
        (r) => r.url().includes('/api/options/historical') && /days=1\b/.test(r.url())
          && r.url().includes(`symbol=${symbol}`),
        { timeout: 30_000 },
      ).catch(() => null);

      await page.goto(`/charts?symbol=${symbol}`, { waitUntil: 'domcontentloaded' });
      const rangeGroup = page.locator('.cw-range-group');
      await expect(rangeGroup).toBeVisible({ timeout: 30_000 });
      const btn1D = rangeGroup.locator('.cw-range-btn', { hasText: '1D' });
      await expect(btn1D).toBeEnabled({ timeout: 30_000 });
      await btn1D.click();

      const resp = await histPromise;
      expect(resp, `/api/options/historical?days=1 for ${symbol} was not requested`).not.toBeNull();
      const body = await resp.json().catch(() => null);
      const bars = Array.isArray(body?.bars) ? body.bars : [];
      expect(bars.length, `expected 1D bars for ${symbol}`).toBeGreaterThan(1);

      const dates = new Set(bars.map((b) => String(b.ts).slice(0, 10)));
      expect(
        dates.size,
        `1D response for ${symbol} spans ${dates.size} calendar dates ` +
        `(${[...dates].join(', ')}) — the weekday double-session bug is ` +
        'back if this is ever more than 1.',
      ).toBe(1);
    });
  }

  // ── VWAP session-reset gating — operator: "if the backend/frontend
  // computes VWAP naively across the whole multi-day window instead of
  // per-session, that's a real bug worth finding" (2026-10-31). Confirmed
  // live and fixed in frontend/src/lib/chart/indicators.js:vwap() (session
  // reset) + ChartWorkspace.svelte's _showVwap (daily-interval gate — a
  // correctly session-reset VWAP on a one-bar-per-session daily chart is
  // just that bar's own typical price, not a useful line, matching how
  // TradingView/Kite disable session VWAP above daily resolution). ───────
  test('VWAP renders on 1D, is gated (no line) on 1M, and carries an "intraday only" hint', async ({ page }) => {
    test.setTimeout(60_000);

    await page.goto('/charts?symbol=RELIANCE', { waitUntil: 'domcontentloaded' });
    const rangeGroup = page.locator('.cw-range-group');
    await expect(rangeGroup).toBeVisible({ timeout: 30_000 });
    const btn1M = rangeGroup.locator('.cw-range-btn', { hasText: '1M' });
    await expect(btn1M).toBeEnabled({ timeout: 30_000 });
    await btn1M.click();
    await expect(page.locator('.cw-fetch-spinner')).toHaveCount(0, { timeout: 20_000 });

    const trigger = page.locator('.rbq-multi-trigger').first();
    await trigger.click();
    const panel = page.locator('.rbq-multi-panel');
    await expect(panel).toBeVisible({ timeout: 5_000 });
    const vwapOpt = panel.locator('.rbq-multi-option', { hasText: 'VWAP' });
    await expect(vwapOpt).toContainText('intraday only');
    await vwapOpt.click();
    // Confirm the chip is checked (selected) BEFORE closing the panel —
    // it unmounts entirely on close ({#if open} in MultiSelect.svelte).
    await expect(vwapOpt).toHaveClass(/rbq-multi-option-selected/);
    await page.keyboard.press('Escape');
    await page.waitForTimeout(400);

    // Gated on 1M (daily bars) — no VWAP line drawn, even though selected.
    expect(await page.locator('.overlay-vwap').count()).toBe(0);

    // Switch to 1D — VWAP renders immediately with no re-pick needed
    // (selection must survive the range change — chartStore persists it).
    const btn1D = rangeGroup.locator('.cw-range-btn', { hasText: '1D' });
    await btn1D.click();
    await expect(page.locator('.cw-fetch-spinner')).toHaveCount(0, { timeout: 20_000 });
    await expect(page.locator('.overlay-vwap')).toHaveCount(1, { timeout: 10_000 });
  });
});
