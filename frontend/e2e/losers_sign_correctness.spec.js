/**
 * Regression spec: Losers grid must render the Day % as NEGATIVE
 * (with a leading `-` or Unicode minus `−`).
 *
 * Why: Operator reported (2026-07-03) that SIEMENS showed +50% in the Losers
 * grid. Investigation confirmed backend + moversStore + column defs all
 * preserve sign end-to-end (SIEMENS was actually -1.06% in the DB snapshot,
 * not +50%), so the report was a stale display, not a code bug. This spec
 * locks the correctness invariant so any future sign-flip (e.g. accidental
 * `Math.abs(change_pct)` in a cell renderer, or a `directional()` misapplied
 * to the left-grid Day % column) is caught at CI.
 *
 * Uses INFY / RELIANCE / TCS / etc (all in FO_UNDERLYINGS) so the 'underlying'
 * mover tab is auto-selected — same left-grid Day % column definition
 * (`left_change_pct` reading raw signed `change_pct` with `pctFmtGrid`) that
 * renders SIEMENS on the midcap tab. Assertions target the DOM text (the
 * exact thing the operator sees) rather than symbol-substring matching so
 * rows without hydrated symbol data still exercise the correctness invariant.
 *
 * Mocked payload:
 *   INFY     = -50%  → must render "-50.00%" in Losers grid
 *   RELIANCE = +50%  → must render "50.00%" in Winners grid (no leading `-`)
 *
 * Five quality dimensions:
 *   SSOT     — one truth (backend change_pct signed) preserved through to grid
 *   Perf     — no extra fetches; single mock covers the whole grid
 *   Stale    — no localStorage residue survives (test clears storage)
 *   Reuse    — mock pattern reused from movers_winners_losers_regression.spec.js
 *   UX       — Day % cell text starts with `-` (ASCII) or `−` (U+2212) for losers,
 *              starts with a digit for winners; no cross-bucket sign contamination
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

// Sign regexes over the concatenated ag-Grid row text.
//   NEG_RE  — matches a negative percentage (e.g. "…-50.00%…" or "…−50.00%…").
//   POS_RE  — matches a positive percentage (e.g. "…50.00%…").
//             Non-numeric, non-minus prefix required so "-50%" doesn't match.
const NEG_RE = /[-−]\d+\.\d+%/;
const POS_RE = /(?:^|[^0-9\-−])\d+\.\d+%/;

async function signIn(page) {
  for (const creds of [
    { user: process.env.PLAYWRIGHT_USER || 'ambore', pass: process.env.PLAYWRIGHT_PASS || 'admin1234' },
    { user: 'rambo', pass: process.env.PLAYWRIGHT_PASS || 'admin1234' },
  ]) {
    try {
      await loginAsAdmin(page, creds);
      return creds.user;
    } catch (_) { /* try next */ }
  }
  throw new Error('Could not sign in');
}

async function mockMovers(page) {
  const make = (sym, pct, last, prev) => ({
    tradingsymbol:  sym,
    exchange:       'NSE',
    last_price:     last,
    previous_close: prev,
    change_pct:     pct,
    peak_pct:       Math.abs(pct) + 0.1,
    sticky:         false,
  });

  // Underlying-tab symbols (FO_UNDERLYINGS in indexConstituents.js) so they
  // auto-land in the default 'underlying' tab without a tab click.
  const movers = [
    // Losers (negative change_pct) — leader has the largest magnitude.
    make('INFY',     -50, 1000, 2000),
    make('WIPRO',    -30,  350,  500),
    make('HCLTECH',   -5,  950, 1000),
    // Winners (positive change_pct).
    make('RELIANCE',  50, 3000, 2000),
    make('TCS',       30, 2600, 2000),
    make('HDFCBANK',   5, 1050, 1000),
  ];

  await page.route('**/api/watchlist/movers', route =>
    route.fulfill({
      status:      200,
      contentType: 'application/json',
      body: JSON.stringify({
        movers,
        threshold_pct: 1.5,
        session_date:  new Date().toISOString().slice(0, 10),
        captured_at:   null,
      }),
    })
  );
}

/** Returns raw text content of all rows in a bucket. */
async function bucketRowTexts(page, cls) {
  return await page.locator(`${cls} .ag-center-cols-container .ag-row`).allTextContents();
}

/** Clear any residual mover cache before navigation. */
async function clearMoverCache(page) {
  await page.addInitScript(() => {
    try {
      sessionStorage.removeItem('mp.selectedShow');
      for (const k of Object.keys(localStorage)) {
        if (k.startsWith('rbq.cache.md.movers')) localStorage.removeItem(k);
      }
    } catch (_) { /* noop */ }
  });
}

/** Core sign-correctness assertions, shared between desktop + mobile. */
function assertSignCorrectness(loserTexts, winnerTexts) {
  // Perf: got at least one row on each side.
  expect(loserTexts.length,  'Losers grid must have >= 1 row').toBeGreaterThanOrEqual(1);
  expect(winnerTexts.length, 'Winners grid must have >= 1 row').toBeGreaterThanOrEqual(1);

  // SSOT — every Losers row MUST render a negative Day %.
  // Direct answer to operator's report ("SIEMENS 50% in losers — should be
  // negative"). If any losers row renders a positive Day %, this fails.
  for (let i = 0; i < loserTexts.length; i++) {
    const t = loserTexts[i];
    expect(
      NEG_RE.test(t),
      `Losers row ${i} MUST have a negative Day % (text: "${t}")`
    ).toBe(true);
  }

  // SSOT — every Winners row MUST render a positive Day % (no leading minus).
  for (let i = 0; i < winnerTexts.length; i++) {
    const t = winnerTexts[i];
    expect(
      NEG_RE.test(t),
      `Winners row ${i} MUST NOT have a negative Day % (text: "${t}")`
    ).toBe(false);
    expect(
      POS_RE.test(t),
      `Winners row ${i} MUST have a positive Day % (text: "${t}")`
    ).toBe(true);
  }

  // Explicit magnitude check — the leader in each bucket is the -50 / +50 row.
  // Catches the specific operator report ("50% in losers should be negative").
  const hasMinusFifty = loserTexts.some(t => /[-−]50\.00%/.test(t));
  const hasPlusFifty  = winnerTexts.some(t => /(?:^|[^0-9\-−])50\.00%/.test(t));
  expect(hasMinusFifty, 'Losers must include a -50.00% row (top loser from mock)').toBe(true);
  expect(hasPlusFifty,  'Winners must include a 50.00% row (top winner from mock)').toBe(true);

  // Cross-bucket contamination check — a -50% row must never appear in Winners,
  // and a +50% row must never appear in Losers.
  const negFiftyInWinners = winnerTexts.some(t => /[-−]50\.00%/.test(t));
  const posFiftyInLosers  = loserTexts.some(t => {
    // Exclude the negative "-50.00%" substring by requiring no leading `-` / `−`.
    return /(?:^|[^0-9\-−])50\.00%/.test(t);
  });
  expect(negFiftyInWinners, 'Winners must NEVER contain a -50.00% row').toBe(false);
  expect(posFiftyInLosers,  'Losers must NEVER contain a positive 50.00% row').toBe(false);
}

test.describe('Movers sign-correctness: Losers render negative Day %, Winners render positive', () => {
  test('desktop — Losers rows all show negative Day %; Winners rows all show positive Day %', async ({ page }) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width: 1440, height: 900 });

    await signIn(page);
    await mockMovers(page);
    await clearMoverCache(page);

    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    // Wait for both grids populated.
    await expect(
      page.locator('.mp-bucket-losers  .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });
    await expect(
      page.locator('.mp-bucket-winners .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });

    const loserTexts  = await bucketRowTexts(page, '.mp-bucket-losers');
    const winnerTexts = await bucketRowTexts(page, '.mp-bucket-winners');

    console.log(`[desktop] losers=${loserTexts.length} winners=${winnerTexts.length}`);
    for (let i = 0; i < loserTexts.length; i++) console.log(`  [L${i}]`, loserTexts[i]);
    for (let i = 0; i < winnerTexts.length; i++) console.log(`  [W${i}]`, winnerTexts[i]);

    assertSignCorrectness(loserTexts, winnerTexts);
  });

  test('mobile portrait — sign-correctness holds on stacked layout', async ({ page }) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width: 390, height: 844 });

    await signIn(page);
    await mockMovers(page);
    await clearMoverCache(page);

    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    await expect(
      page.locator('.mp-bucket-losers  .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });
    await expect(
      page.locator('.mp-bucket-winners .ag-center-cols-container .ag-row').first()
    ).toBeVisible({ timeout: 60_000 });

    const loserTexts  = await bucketRowTexts(page, '.mp-bucket-losers');
    const winnerTexts = await bucketRowTexts(page, '.mp-bucket-winners');

    console.log(`[mobile] losers=${loserTexts.length} winners=${winnerTexts.length}`);

    assertSignCorrectness(loserTexts, winnerTexts);
  });
});
