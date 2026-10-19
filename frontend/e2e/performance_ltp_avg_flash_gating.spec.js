/**
 * performance_ltp_avg_flash_gating.spec.js
 *
 * PerformancePage's LTP (`last_price`, holdingsCols/positionsCols ~610/741)
 * and Avg (`average_price`, via `avgClsWithDir`) cells used to flash on
 * EVERY real-time SSE tick for a held symbol (`tickBus.subscribe` driving
 * `_perfLtpFlashUp`/`_perfLtpFlashDown`, consumed as a priority branch
 * inside `avgVsLtpCls`), even though nothing on this page ever patches
 * `last_price`/`average_price` per-tick — both fields are poll-only,
 * written only by `loadAll()`/`applyData()`/`applyAccountFilter()` (via
 * `_seedFlash`) or by `_applyFillDelta()` on a real fill. The cell visibly
 * pulsed while its own displayed number never moved.
 *
 * Fix (2026-10-19): the tick-bus-priority branch was removed from
 * `avgVsLtpCls` — LTP now flashes ONLY off `_perfFlash.classOf(':last_price')`,
 * which `tickFlash.svelte.js`'s own `v === last` guard only sets when the
 * polled value genuinely changed. Avg previously inherited whatever flash
 * class `avgVsLtpCls` produced for LTP (so it flashed on every LTP poll-diff
 * even when `average_price` itself hadn't moved) — `avgClsWithDir` now
 * strips `ltp-tc-flash-*` from the inherited base and sources its OWN flash
 * from a new `:average_price` key (seeded alongside `:last_price` in
 * `_seedFlash`), so Avg only pulses when the cost basis itself changes.
 *
 * Five quality dimensions:
 *  1. SSOT     — one flash source per displayed value: `:last_price` drives
 *                the LTP cell, `:average_price` drives the Avg cell — no
 *                cross-wiring between the two.
 *  2. Perf     — no new network calls; reuses the existing poll cycle
 *                (`loadAll()`/`applyAccountFilter()`/`_seedFlash`).
 *  3. Stale    — a poll landing with an UNCHANGED value must never leave a
 *                stray flash class.
 *  4. Reusable — same `createTickFlash()`/`tf-up`/`tf-down` → `ltp-tc-flash-*`
 *                convention already used by OptionsPayoff/MarketPulse.
 *  5. UX       — a cell only ever visibly pulses when the number it shows
 *                actually changed; a changed LTP does not "borrow" a flash
 *                onto the unrelated Avg cell.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/performance_ltp_avg_flash_gating.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

const ACCOUNT = 'ZG0790';
const SYMBOL  = 'NIFTY26DEC25000CE';

function _positionsBody(lastPrice) {
  return JSON.stringify({
    rows: [{
      tradingsymbol: SYMBOL, symbol: SYMBOL, exchange: 'NFO', account: ACCOUNT,
      quantity: 50, opening_quantity: 50, overnight_quantity: 50,
      day_buy_quantity: 0, day_sell_quantity: 0, day_sell_value: 0,
      average_price: 200.0, last_price: lastPrice, close_price: 195.0,
      pnl: 500.0, day_change_val: 50.0, day_change_percentage: 2.5,
      unrealised_pnl: 500.0, realised_pnl: 0, source: 'live', product: 'NRML',
    }],
    summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null,
  });
}

const EMPTY_BOOK = JSON.stringify({ rows: [], summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null });

/** Arms the mocked HTTP routes PerformancePage's loadAll() hits. `state.lastPrice`
 *  is read fresh on every /api/positions/ request, so the test can change it
 *  between polls without re-arming the route. */
async function _armRoutes(page, state) {
  await Promise.all([
    page.route('**/api/positions**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: _positionsBody(state.lastPrice) });
    }),
    page.route('**/api/holdings**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: EMPTY_BOOK });
    }),
    page.route('**/api/funds**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: EMPTY_BOOK });
    }),
    page.route('**/api/nav/by-account**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows: [] }) });
    }),
  ]);
}

/** Navigate to /performance, wait for the positions row to render, and
 *  return the WS route for /ws/performance (null if it never connected)
 *  plus the LTP/Avg cell locators for the one mocked row. */
async function _gotoAndWaitForRow(page, state) {
  /** @type {import('@playwright/test').WebSocketRoute | null} */
  let wsRoute = null;
  await page.routeWebSocket('**/ws/performance', (ws) => { wsRoute = ws; });
  await _armRoutes(page, state);
  await page.goto('/performance', { waitUntil: 'domcontentloaded' });

  // The grid renders a Dhan-hyphenated display form (e.g.
  // "NIFTY-29DEC26-25000-CE") for the raw tradingsymbol via
  // `_fmtSymCached`/`_posSymRenderer` — the raw symbol only survives as a
  // `data-sym` attribute, not as visible text. Match on that instead of
  // `hasText`.
  //
  // ag-Grid renders pinned-left columns (Symbol) and scrollable "center"
  // columns (LTP, Avg, ...) as TWO SEPARATE `.ag-row` DOM elements (one
  // per section), not children of one shared row div — a plain
  // `row.locator(...)` scoped to the pinned-left row element can never
  // find the center-column cells. Find the pinned row's `row-id` (shared
  // across both sections) and re-scope the LTP/Avg lookups to it.
  const pinnedRow = page.locator(`.ag-row:has([data-sym="${SYMBOL}"])`).first();
  await expect(pinnedRow).toBeVisible({ timeout: 15_000 });
  const rowId = await pinnedRow.getAttribute('row-id');
  const gridRoot = pinnedRow.locator('xpath=ancestor::*[contains(@class,"ag-root-wrapper")]').first();
  const ltpCell = gridRoot.locator(`.ag-row[row-id="${rowId}"] [col-id="last_price"]`);
  const avgCell = gridRoot.locator(`.ag-row[row-id="${rowId}"] [col-id="average_price"]`);
  await expect(ltpCell).toBeVisible({ timeout: 10_000 });

  for (let i = 0; i < 25 && !wsRoute; i++) await page.waitForTimeout(200);
  return { wsRoute, ltpCell, avgCell };
}

/** Poll for a flash class appearing on a locator over `windowMs`. */
async function _pollForFlash(locator, windowMs = 2_000) {
  const deadline = Date.now() + windowMs;
  while (Date.now() < deadline) {
    const cls = await locator.getAttribute('class');
    if (cls && /ltp-tc-flash-(up|down)/.test(cls)) return true;
    await locator.page().waitForTimeout(40);
  }
  return false;
}

/** Sample BOTH locators on every tick of the same poll loop (not two
 *  sequential loops) — the flash window is only ~300-400ms, so checking
 *  one locator to exhaustion before starting the other would miss a
 *  transient class on the second one entirely. Returns { a, b } booleans:
 *  whether each locator was EVER seen carrying a flash class during the
 *  window. */
async function _pollForFlashBoth(locA, locB, windowMs = 2_000) {
  const deadline = Date.now() + windowMs;
  let a = false, b = false;
  while (Date.now() < deadline) {
    const [clsA, clsB] = await Promise.all([locA.getAttribute('class'), locB.getAttribute('class')]);
    if (clsA && /ltp-tc-flash-(up|down)/.test(clsA)) a = true;
    if (clsB && /ltp-tc-flash-(up|down)/.test(clsB)) b = true;
    if (a) break; // LTP flash observed — enough samples already taken of B in lockstep
    await locA.page().waitForTimeout(40);
  }
  return { a, b };
}

test.describe('PerformancePage — LTP/Avg flash only fires on a genuine displayed-value change', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('two polls with an unchanged last_price never flash LTP or Avg', async ({ page }) => {
    const state = { lastPrice: 210.0 };
    const { wsRoute, ltpCell, avgCell } = await _gotoAndWaitForRow(page, state);
    if (!wsRoute) {
      test.skip(true, 'WebSocket to /ws/performance never connected — cannot drive a second poll deterministically');
      return;
    }

    // Baseline poll already landed on page load (first sample — no flash
    // by design). Trigger a SECOND poll with the SAME last_price via the
    // 'positions_refreshed' WS event (routes applyData()/applyAccountFilter()
    // through the exact same code path a real re-poll would).
    wsRoute.send(JSON.stringify({ event: 'positions_refreshed', refreshed_at: new Date().toISOString() }));

    const sawLtpFlash = await _pollForFlash(ltpCell, 1_500);
    const sawAvgFlash = await _pollForFlash(avgCell, 1_500);
    expect(sawLtpFlash, 'LTP must not flash when last_price is unchanged between polls').toBe(false);
    expect(sawAvgFlash, 'Avg must not flash when average_price is unchanged between polls').toBe(false);
  });

  test('a changed last_price flashes LTP but not the unrelated Avg cell', async ({ page }) => {
    const state = { lastPrice: 210.0 };
    const { wsRoute, ltpCell, avgCell } = await _gotoAndWaitForRow(page, state);
    if (!wsRoute) {
      test.skip(true, 'WebSocket to /ws/performance never connected — cannot drive a second poll deterministically');
      return;
    }

    // Change the value the NEXT /api/positions/ response will carry, then
    // trigger the second poll. average_price stays at 200.0 throughout —
    // only last_price genuinely changes.
    state.lastPrice = 225.0;
    wsRoute.send(JSON.stringify({ event: 'positions_refreshed', refreshed_at: new Date().toISOString() }));

    // Sample LTP and Avg TOGETHER on every poll tick — the flash window is
    // only ~300-400ms, so checking LTP to exhaustion first would miss a
    // transient class on Avg (the exact bug this spec guards against: Avg
    // inheriting LTP's flash class in the same render cycle).
    const { a: sawLtpFlash, b: sawAvgFlash } = await _pollForFlashBoth(ltpCell, avgCell, 2_000);
    expect(sawLtpFlash, 'LTP must flash when last_price genuinely changes between polls').toBe(true);
    expect(sawAvgFlash, 'Avg must not flash off an unrelated LTP change (average_price never moved)').toBe(false);
  });
});
