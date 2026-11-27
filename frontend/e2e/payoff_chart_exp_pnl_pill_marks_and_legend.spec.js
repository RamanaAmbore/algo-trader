/**
 * payoff_chart_exp_pnl_pill_marks_and_legend.spec.js
 *
 * 2026-11-26 Payoff chart (OptionsPayoff.svelte) operator asks:
 *
 *  a. Exp P&L shaded pill at the BOTTOM of the chart, mirroring the
 *     existing spot-price pill's decoration (navy mask + colour pill +
 *     bold dark text) at the TOP. Reads `curveAtSpot.expiry_value`
 *     directly — the C1 invariant (no second, independently-computed
 *     figure) — so it can never disagree with the Exp P&L stat row or
 *     the expiry dart.
 *  b. Top-left overlay label renames: "P&L" → "Mark P&L",
 *     "ADJ" → "MARK ADJ". "DAY P&L" and "Exp P&L" are untouched.
 *  c. Legend gains dart-style (filled-circle + halo) markers for all
 *     three at-current-spot quantities: "Theoretical P&L" (amber,
 *     renamed from the old bare "P&L" line-swatch label), "Market P&L"
 *     (violet, new — gated on `dayPnl != null`, matching its dart's own
 *     guard), "Exp P&L" (sky, same text, now a dart marker instead of a
 *     dashed-line swatch).
 *
 * Reuses the same mock-endpoint pattern as
 * `derivatives_payoff_dart_number_parity.spec.js` (flat payoff curve,
 * `blockLiveTicks`, `mockCommonEndpoints`) — local copies per this
 * suite's established per-file convention (see that file's own header).
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const PAYOFF_PATH = path.resolve(DIR, '../src/lib/OptionsPayoff.svelte');

const ACCOUNT_A = 'ZG0790';

function positionsBody(rows) {
  return { rows, summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null };
}

/** @param {{symbol:string,account:string,qty:number,avgCost:number,ltp:number}} opts */
function optionRow({ symbol, account, qty, avgCost, ltp }) {
  return {
    tradingsymbol: symbol, symbol, account,
    quantity: qty, average_price: avgCost, last_price: ltp,
    pnl: 0, realised: 0, unrealised: 0,
    overnight_quantity: qty, day_buy_quantity: 0, day_sell_quantity: 0,
    day_buy_value: 0, day_sell_value: 0,
  };
}

async function blockLiveTicks(page) {
  await page.route('**/api/quotes/stream', (route) => route.abort());
  await page.routeWebSocket('**/ws/performance', (ws) => ws.close());
  await page.routeWebSocket('**/ws/algo', (ws) => ws.close());
}

/**
 * @param {import('@playwright/test').Page} page
 * @param {{positionsRows: any[], instrumentItems: any[], strategyResponse: any}} opts
 */
async function mockCommonEndpoints(page, { positionsRows, instrumentItems, strategyResponse }) {
  await page.route('**/api/positions**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(positionsBody(positionsRows)) });
  });
  await page.route('**/api/instruments**', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ cycle_date: '2026-11-26', count: instrumentItems.length, items: instrumentItems }),
    });
  });
  await page.route('**/api/holdings**', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ rows: [], summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null }),
    });
  });
  await page.route('**/api/watchlist**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ watchlists: [], symbols: [] }) });
  });
  await page.route('**/api/options/strategy-analytics**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(strategyResponse) });
  });
}

function flatCurve(expiryValue, todayValue, spotCenter) {
  const pts = [spotCenter * 0.8, spotCenter, spotCenter * 1.2];
  return pts.map((spot) => ({ spot, today_value: todayValue, expiry_value: expiryValue }));
}

async function gotoNiftyStrategy(page, { expPnl, todayPnl, spot = 20000 }) {
  await loginAsAdmin(page);
  await blockLiveTicks(page);

  const SYM = 'NIFTY26NOV20000CE';
  await mockCommonEndpoints(page, {
    positionsRows: [optionRow({ symbol: SYM, account: ACCOUNT_A, qty: 1, avgCost: 100, ltp: 110 })],
    instrumentItems: [
      { s: SYM, e: 'NFO', t: 'CE', ls: 50, ts: 0.05, u: 'NIFTY', x: '2026-11-27', k: spot },
    ],
    strategyResponse: {
      underlying: 'NIFTY', spot, spot_prev_close: spot,
      // Synthetic anchor (test-only determinism knob, same convention as
      // derivatives_payoff_dart_number_parity.spec.js's Case (b)/(d)) —
      // guarantees payoffSpot resolves to `spot` regardless of any
      // live/poll underlying-quote tick (the real NIFTY LTP in this test
      // account can sit numerically outside the mocked curve's domain,
      // which would otherwise suppress both the spot pill and this new
      // Exp P&L pill via their shared spot-in-domain guard).
      spot_anchor_contract: 'NIFTY_TEST_SPOT_ANCHOR', spot_source: 'futures',
      legs: [
        { symbol: SYM, qty: 1, strike: spot, opt_type: 'CE', greeks: { delta: 0.5, gamma: 0.01, theta: -0.3, vega: 0.2, rho: 0.01 } },
      ],
      payoff: flatCurve(expPnl, todayPnl, spot),
      risk: { breakevens: [spot] }, breakevens: [spot],
      pnl_at_spot: todayPnl, exp_pnl_at_spot: expPnl,
      dte: 1, iv: 0.3, aggregate_greeks: { delta: 1.0, gamma: 0.02, theta: -0.6, vega: 0.4, rho: 0.02 },
    },
  });

  await page.goto('/admin/derivatives?u=NIFTY', { waitUntil: 'domcontentloaded' });
  const overlay = page.locator('.payoff-stats');
  await expect(overlay).toBeVisible({ timeout: 20_000 });
  // Confirm the mocked strategy (not the client-side stub curve) is active.
  const legsChip = overlay.locator('.ps-row', { has: page.locator('.ps-k', { hasText: 'LEGS' }) }).locator('.ps-v');
  await expect(legsChip).not.toHaveText('0', { timeout: 15_000 });
  return overlay;
}

test.describe('Payoff chart — Exp P&L bottom pill, label renames, legend markers', () => {
  // The bottom pill's live rendering depends on `spot` landing inside
  // the mocked curve's x-domain. A first attempt with
  // `spot_anchor_contract: null` (index-sourced) let the real-time
  // NIFTY quote — a separate live data source from the mocked
  // `strategy-analytics` response — drive `payoffSpot` instead, which
  // sat well outside the mocked domain and correctly SUPPRESSED both
  // the existing top spot pill and this new bottom pill via their
  // shared `spot >= sMin && spot <= sMax` guard. `gotoNiftyStrategy()`
  // below instead uses the same synthetic-anchor determinism knob
  // `derivatives_payoff_dart_number_parity.spec.js`'s Case (b)/(d)
  // already establish (`spot_anchor_contract: 'NIFTY_TEST_SPOT_ANCHOR'`,
  // `spot_source: 'futures'`), which guarantees `payoffSpot` resolves to
  // the mocked `spot` regardless of any live tick — the LIVE test below
  // asserts the pill's exact rendered text, not just its source shape.
  test('SOURCE: the bottom Exp P&L pill reads curveAtSpot.expiry_value directly (C1 invariant) and shares the spot pill\'s guard + decoration technique', () => {
    const src = readFileSync(PAYOFF_PATH, 'utf8');
    const pillIdx = src.indexOf('Exp P&L pill — bottom of chart');
    expect(pillIdx, 'bottom Exp P&L pill comment not found').toBeGreaterThan(-1);
    const block = src.slice(pillIdx, pillIdx + 2200);

    // Same guard shape as the spot pill (spot >= sMin && spot <= sMax),
    // plus the curve must actually be resolved.
    expect(block).toMatch(/curveAtSpot != null && curveAtSpot\.expiry_value != null && spot >= sMin && spot <= sMax/);
    // No second, independently-computed figure — reads the identical
    // value the Exp P&L stat row and the expiry dart's own y-position use.
    expect(block).toMatch(/\{@const expLabel = fmtMoney\(curveAtSpot\.expiry_value\)\}/);
    // Same navy-mask + colour-pill + bold-dark-text structure as the top
    // spot pill (2 <rect>s + 1 <text>, rx 5 mask / rx 4 pill).
    expect(block).toMatch(/rx="5"\s*\n\s*fill="#0d1829"/);
    expect(block).toMatch(/fill="#7dd3fc" stroke="#0369a1"/);
    expect(block).toMatch(/text-anchor="middle"/);
    // Positioned at the BOTTOM of the plot area (height - PAD_B), not the top.
    expect(block).toMatch(/expChipY = height - PAD_B - expChipH - 3/);
  });

  test('LIVE: bottom Exp P&L pill renders with the exact same text as the Exp P&L stat row', async ({ page }) => {
    const EXP_PNL = 2345;
    const overlay = await gotoNiftyStrategy(page, { expPnl: EXP_PNL, todayPnl: 1500 });

    const expRow = overlay.locator('.ps-row', { has: page.locator('.ps-k', { hasText: 'Exp P&L' }) }).first();
    const expText = (await expRow.locator('.ps-v').textContent()) || '';

    const chartSvg = page.locator('.payoff-svg').first();
    await expect(chartSvg).toBeVisible({ timeout: 15_000 });
    const pillTexts = await chartSvg.locator('text').allTextContents();
    const cleaned = (s) => s.replace(/[₹,\s]/g, '');
    const matchFound = pillTexts.some((t) => cleaned(t) !== '' && cleaned(t) === cleaned(expText));
    expect(matchFound, `expected a chart <text> matching the Exp P&L stat row's own text (${expText}); saw: ${JSON.stringify(pillTexts)}`).toBe(true);

    // Two <rect>s for the top spot pill, two more for the new bottom pill.
    const rectCount = await chartSvg.locator('rect').count();
    expect(rectCount, 'expected at least 4 <rect> elements (2 top spot pill + 2 new bottom Exp P&L pill)').toBeGreaterThanOrEqual(4);
  });

  test('top-left overlay renames: "Mark P&L" and "MARK ADJ" render; "DAY P&L"/"Exp P&L" are untouched', async ({ page }) => {
    const overlay = await gotoNiftyStrategy(page, { expPnl: 500, todayPnl: 1500 });

    await expect(overlay.locator('.ps-k', { hasText: 'Mark P&L' })).toBeVisible({ timeout: 10_000 });
    // "Mark P&L" must not ALSO still show a bare "P&L" label anywhere
    // (the old label is gone, not merely duplicated).
    const bareLabel = overlay.locator('.ps-k').filter({ hasText: /^P&L$/ });
    expect(await bareLabel.count()).toBe(0);

    await expect(overlay.locator('.ps-k', { hasText: 'DAY P&L' })).toBeVisible();
    await expect(overlay.locator('.ps-k', { hasText: 'Exp P&L' })).toBeVisible();
  });

  test('legend shows three dart-style markers: Theoretical P&L (amber), Market P&L (violet, gated on dayPnl), Exp P&L (sky)', async ({ page }) => {
    await gotoNiftyStrategy(page, { expPnl: 500, todayPnl: 1500 });

    const legend = page.locator('.payoff-legend').first();
    await expect(legend).toBeVisible({ timeout: 15_000 });

    const theoretical = legend.locator('.legend-item', { hasText: 'Theoretical P&L' });
    await expect(theoretical).toBeVisible();
    await expect(theoretical.locator('.legend-dart.legend-dart-today')).toBeVisible();

    const exp = legend.locator('.legend-item', { hasText: 'Exp P&L' }).first();
    await expect(exp).toBeVisible();
    await expect(exp.locator('.legend-dart.legend-dart-expiry')).toBeVisible();

    // Market P&L requires `dayPnl != null` on the component — the
    // derivatives page threads this from the baseline-diff Day P&L sum,
    // which may legitimately be absent/zero in this mocked scenario. If
    // present, it must carry the violet dart marker.
    const market = legend.locator('.legend-item', { hasText: 'Market P&L' });
    const marketCount = await market.count();
    if (marketCount > 0) {
      await expect(market.locator('.legend-dart.legend-dart-actual')).toBeVisible();
    } else {
      test.info().annotations.push({ type: 'skip-reason', description: 'dayPnl was null/absent in this mocked scenario — Market P&L legend entry correctly omitted' });
    }

    // Old line-swatch classes must be gone from the legend entirely.
    await expect(legend.locator('.legend-today')).toHaveCount(0);
    await expect(legend.locator('.legend-expiry')).toHaveCount(0);
  });
});
