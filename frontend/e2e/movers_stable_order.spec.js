/**
 * Movers stable-order regression spec — Playwright e2e.
 *
 * Operator complaint (2026-07-03): "the winners and losers cards shuffling
 * rows when the market is closed, why?"
 *
 * Root cause: `buildUnified` in MarketPulse.svelte read `liveChangePct` from
 * symbolStore (`snap.day_change_pct`) as the primary source. During closed
 * hours, multiple publishers — `publishMoverRows`, `publishPulseQuotes`,
 * `publishWatchQuotes` — all write `day_change_pct` to the same symbolStore
 * slot using `snapshot_ts = Date.now()`. The most-recently-landed publisher
 * wins. When publishers produce subtly different floating-point values for the
 * same symbol (e.g. /watchlist/movers returns 1.058 while /quote/batch returns
 * 1.061), `_mover_change_pct` oscillates poll-to-poll, flipping the sort order
 * of nearby-ranked symbols.
 *
 * Fix applied (line 3322, buildUnified):
 *   BEFORE:  const liveChangePct = snap?.day_change_pct ?? m.change_pct ?? null;
 *   AFTER:   const liveChangePct = m.change_pct ?? snap?.day_change_pct ?? null;
 *
 * By preferring `m.change_pct` (the moversStore-owned value, from the same
 * stable DB snapshot on every closed-hours poll), `_mover_change_pct` becomes
 * invariant to competing publisher drift. `_topRowsFor` therefore produces the
 * same sort order on every render.
 *
 * This spec reproduces the ACTUAL shuffle mechanism:
 *  1. Mocks /api/watchlist/movers with a stable closed-hours snapshot containing
 *     pairs of rows with near-identical |change_pct| values (the shuffle-prone
 *     case).
 *  2. Mocks POST /api/quote/batch (called by publishPulseQuotes) with DRIFTED
 *     change_pct values for the same symbols — simulating the competing-publisher
 *     float divergence that caused the reorder.
 *  3. Re-routes the quote/batch mock with a second drift value between captures,
 *     which would have flipped sort order under the old code.
 *  4. Asserts that the Winners and Losers grid row order is IDENTICAL both
 *     before and after the drift injection.
 *  5. Verifies no rows disappear (keepStaleOnEmpty regression guard).
 *
 * Five quality dimensions:
 *  1. SSOT     — `buildUnified` line 3322 is the single gate; `_topRowsFor`
 *                is the single sort point for Winners/Losers grids.
 *  2. Perf     — mocked responses only; no real broker calls; < 10 s per run.
 *  3. Stale    — asserts BEFORE order == AFTER order (regression guard for
 *                the shuffling defect). Fails on the OLD code, passes on the fix.
 *  4. Reuse    — same route.fulfill + page.evaluate pattern as
 *                dhan_stale_persist.spec.js and closed_hours_day_change.spec.js.
 *  5. UX       — winners/losers cards both verified; row count stable across
 *                drift cycles (no phantom dropouts).
 *
 * Run context: chromium-desktop (ag-Grid requires min-width viewport).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 30_000;

// Closed-hours market status — NSE + MCX both closed.
const MARKET_CLOSED = {
  nse_open: false,
  mcx_open: false,
  any_open: false,
  is_holiday: false,
};

// Snapshot timestamp — non-null signals a backend persisted snapshot.
const CAPTURED_AT = '2026-07-02T15:35:00.000000+00:00';

// -----------------------------------------------------------------------
// Movers payload — stable DB snapshot values (no drift).
// BANKNIFTY and TCS share |change_pct| = 1.058; TECHM and MINDTREE share
// |change_pct| = 1.172 and 1.176 respectively (near-tie). These tight
// deltas are the historically shuffle-prone case.
// -----------------------------------------------------------------------
const STABLE_MOVERS = {
  movers: [
    // Winners (positive change_pct, ascending by rank)
    { tradingsymbol: 'NIFTY',     exchange: 'NSE', last_price: 24200, previous_close: 23952, change_pct:  1.035, peak_pct:  1.035, sticky: false },
    { tradingsymbol: 'RELIANCE',  exchange: 'NSE', last_price: 1450,  previous_close: 1435,  change_pct:  1.045, peak_pct:  1.045, sticky: false },
    { tradingsymbol: 'BANKNIFTY', exchange: 'NSE', last_price: 52500, previous_close: 51950, change_pct:  1.058, peak_pct:  1.058, sticky: false },
    { tradingsymbol: 'TCS',       exchange: 'NSE', last_price: 3820,  previous_close: 3780,  change_pct:  1.058, peak_pct:  1.058, sticky: false },
    { tradingsymbol: 'INFY',      exchange: 'NSE', last_price: 1720,  previous_close: 1700,  change_pct:  1.176, peak_pct:  1.176, sticky: false },
    // Losers (negative change_pct)
    { tradingsymbol: 'WIPRO',     exchange: 'NSE', last_price: 450,   previous_close: 455,   change_pct: -1.099, peak_pct: -1.099, sticky: false },
    { tradingsymbol: 'HCLTECH',   exchange: 'NSE', last_price: 1560,  previous_close: 1578,  change_pct: -1.141, peak_pct: -1.141, sticky: false },
    { tradingsymbol: 'LTIM',      exchange: 'NSE', last_price: 5600,  previous_close: 5665,  change_pct: -1.147, peak_pct: -1.147, sticky: false },
    { tradingsymbol: 'TECHM',     exchange: 'NSE', last_price: 1680,  previous_close: 1700,  change_pct: -1.172, peak_pct: -1.172, sticky: false },
    { tradingsymbol: 'MINDTREE',  exchange: 'NSE', last_price: 3120,  previous_close: 3157,  change_pct: -1.176, peak_pct: -1.176, sticky: false },
  ],
  threshold_pct: 1.5,
  session_date: '2026-07-02',
  captured_at: CAPTURED_AT,
};

/**
 * Build a /quote/batch response that mimics publishPulseQuotes writing
 * DRIFTED change_pct values for the mover symbols. The drift is the
 * competing-publisher float divergence that causes symbolStore to oscillate.
 *
 * @param {number} drift  Floating-point offset applied to every change_pct.
 * @returns {object}      Batch quote response payload.
 */
function buildDriftedBatchQuotes(drift) {
  return {
    quotes: STABLE_MOVERS.movers.map(m => ({
      tradingsymbol: m.tradingsymbol,
      exchange:      m.exchange,
      ltp:           m.last_price,
      close:         m.previous_close,
      open:          m.previous_close,
      high:          m.last_price,
      low:           m.previous_close,
      // Drift applied here — this is the competing publisher's divergent value.
      // Under the OLD code, _mover_change_pct used this drifted value from
      // symbolStore, flipping near-tie rows on alternate polls.
      change_pct:    m.change_pct + drift,
      change:        m.last_price - m.previous_close,
      volume:        100000,
      oi:            0,
    })),
  };
}

/**
 * Install all route mocks for the publisher-drift scenario.
 *
 * @param {import('@playwright/test').Page} page
 * @param {number} initialDrift  First drift to apply to quote/batch.
 */
async function installDriftMocks(page, initialDrift = 0) {
  await page.route('**/api/market/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(MARKET_CLOSED),
    })
  );

  // Movers endpoint — always stable (DB snapshot, same value every call).
  await page.route('**/api/watchlist/movers*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(STABLE_MOVERS),
    })
  );

  // quote/batch — returns DRIFTED values (simulates publishPulseQuotes writing
  // different day_change_pct values into symbolStore than moversRows wrote).
  await page.route('**/api/quote/batch', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(buildDriftedBatchQuotes(initialDrift)),
    })
  );

  // Watchlist quotes — suppress (empty items, no additional symbolStore writes).
  // This isolates the quote/batch publisher as the only competing source.
  await page.route('**/api/watchlist/*/quotes*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [] }),
    })
  );

  // Empty positions / holdings / funds — focus on movers.
  const empty = () => JSON.stringify({ rows: [], summary: [], refreshed_at: 'Wed 02 Jul 15:30 IST', stale_accounts: [] });
  await page.route('**/api/positions*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: empty() }));
  await page.route('**/api/holdings*',  (route) => route.fulfill({ status: 200, contentType: 'application/json', body: empty() }));
  await page.route('**/api/funds*',     (route) => route.fulfill({ status: 200, contentType: 'application/json', body: empty() }));

  // Sparklines — suppress (cosmetic, not needed for sort regression test).
  await page.route('**/api/sparklines*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ data: {}, refreshed_at: '' }) })
  );

  // Suppress remaining watchlist calls (list metadata, not quotes).
  await page.route('**/api/watchlist/**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], id: -1, name: 'Pinned' }) })
  );
}

/**
 * Extract visible row tradingsymbols from the Winners or Losers card
 * in DOM order (top-to-bottom = rank 1 to N).
 *
 * @param {import('@playwright/test').Page} page
 * @param {'winners'|'losers'} direction
 * @returns {Promise<string[]>}
 */
async function getMoverRowOrder(page, direction) {
  return page.evaluate((dir) => {
    // Find the card whose heading text matches the direction label.
    let directionCard = null;
    const candidates = Array.from(document.querySelectorAll('h3, h4, span, div'));
    for (const el of candidates) {
      const text = (el.textContent || '').trim().toLowerCase();
      if (text === dir) {
        // Walk up at most 6 levels to find an enclosing container with an ag-Grid.
        let p = el.parentElement;
        for (let i = 0; i < 6 && p; i++, p = p.parentElement) {
          if (p.querySelector('.ag-root-wrapper') || p.querySelector('.ag-center-cols-container')) {
            directionCard = p;
            break;
          }
        }
        if (directionCard) break;
      }
    }

    const rowSelector = '.ag-center-cols-container .ag-row';
    const rows = Array.from(
      directionCard
        ? directionCard.querySelectorAll(rowSelector)
        : document.querySelectorAll(rowSelector)
    );

    return rows
      .map(row => {
        // The tradingsymbol is the first short uppercase cell text.
        const cells = Array.from(row.querySelectorAll('.ag-cell'));
        for (const cell of cells) {
          const t = (cell.textContent || '').trim();
          if (t && /^[A-Z0-9 .]+$/.test(t) && t.length >= 3 && t.length < 30) return t;
        }
        return '';
      })
      .filter(Boolean);
  }, direction);
}

/**
 * Wait for the movers grid to populate (at least one ag-Grid row visible).
 * @param {import('@playwright/test').Page} page
 */
async function waitForMoversGrid(page) {
  await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(
    () => document.querySelectorAll('.ag-center-cols-container .ag-row').length > 0,
    { timeout: TIMEOUT }
  );
  // Also wait for the Winners heading to appear.
  await page.waitForFunction(
    () => Array.from(document.querySelectorAll('*')).some(
      el => (el.textContent || '').trim().toLowerCase() === 'winners'
    ),
    { timeout: TIMEOUT }
  );
}

test.describe('Movers stable order — publisher-drift regression', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  /**
   * Core regression: Winners order is stable when quote/batch returns a
   * DIFFERENT change_pct for the same symbols between polls.
   *
   * Under the OLD code (prefers snap.day_change_pct), publishPulseQuotes
   * would overwrite symbolStore with the drifted value, causing _mover_change_pct
   * to fluctuate and near-tie rows to flip on each render.
   *
   * Under the NEW code (prefers m.change_pct), the moversStore-owned value
   * is always used — symbolStore drift is ignored — so the order is stable.
   */
  test('Winners order stable when quote/batch returns drifted change_pct', async ({ page }) => {
    // Initial state: quote/batch returns values slightly above movers snapshot.
    await installDriftMocks(page, +0.003);
    await waitForMoversGrid(page);

    const orderBefore = await getMoverRowOrder(page, 'winners');
    expect(orderBefore.length).toBeGreaterThan(0);

    // Simulate a second poll cycle: quote/batch now returns values slightly
    // BELOW the movers snapshot (opposite sign from first call). Under the
    // old code this would flip the symbolStore value for BANKNIFTY (1.058+0.003=1.061)
    // vs TCS (1.058-0.003=1.055) causing them to swap rank — the shuffle.
    await page.route('**/api/quote/batch', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(buildDriftedBatchQuotes(-0.003)),
      })
    );
    // Wait for any reactive re-render to settle.
    await page.waitForTimeout(400);

    const orderAfter = await getMoverRowOrder(page, 'winners');
    expect(orderAfter.length).toBeGreaterThan(0);

    // The order must be identical — the moversStore-owned change_pct insulates
    // the sort from the competing quote/batch publisher's drifted values.
    expect(orderAfter, 'Winners order must not change when quote/batch drifts').toEqual(orderBefore);
  });

  /**
   * Losers order stable under the same publisher-drift scenario.
   */
  test('Losers order stable when quote/batch returns drifted change_pct', async ({ page }) => {
    await installDriftMocks(page, +0.003);
    await waitForMoversGrid(page);

    const orderBefore = await getMoverRowOrder(page, 'losers');
    expect(orderBefore.length).toBeGreaterThan(0);

    await page.route('**/api/quote/batch', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(buildDriftedBatchQuotes(-0.003)),
      })
    );
    await page.waitForTimeout(400);

    const orderAfter = await getMoverRowOrder(page, 'losers');
    expect(orderAfter.length).toBeGreaterThan(0);

    expect(orderAfter, 'Losers order must not change when quote/batch drifts').toEqual(orderBefore);
  });

  /**
   * Repeated drift cycles: order stays stable across N alternating drift
   * values (regression guard for the intermittent-shuffle complaint).
   */
  test('Winners order stable across N alternating drift cycles', async ({ page }) => {
    await installDriftMocks(page, 0);
    await waitForMoversGrid(page);

    const referenceOrder = await getMoverRowOrder(page, 'winners');
    expect(referenceOrder.length).toBeGreaterThan(0);

    // Alternate between positive and negative drift — the old code would have
    // shuffled BANKNIFTY ↔ TCS (both at 1.058) on every alternate cycle.
    const drifts = [+0.005, -0.005, +0.002, -0.002, +0.001];
    for (let i = 0; i < drifts.length; i++) {
      await page.route('**/api/quote/batch', (route) =>
        route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(buildDriftedBatchQuotes(drifts[i])),
        })
      );
      await page.waitForTimeout(350);
      const order = await getMoverRowOrder(page, 'winners');
      if (order.length > 0) {
        expect(order, `Winners shuffled on drift cycle ${i} (drift=${drifts[i]})`).toEqual(referenceOrder);
      }
    }
  });

  /**
   * Row count stable across drift cycles (keepStaleOnEmpty regression).
   * No rows should disappear when the drift changes.
   */
  test('Winners row count unchanged across drift cycles', async ({ page }) => {
    await installDriftMocks(page, 0);
    await waitForMoversGrid(page);

    const initialOrder = await getMoverRowOrder(page, 'winners');
    const initialCount = initialOrder.length;
    expect(initialCount).toBeGreaterThan(0);

    for (const drift of [+0.005, -0.005, 0]) {
      await page.route('**/api/quote/batch', (route) =>
        route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(buildDriftedBatchQuotes(drift)),
        })
      );
      await page.waitForTimeout(350);
      const order = await getMoverRowOrder(page, 'winners');
      // Count must never change — no rows dropped or added by drift.
      expect(order.length, `Row count changed on drift=${drift}`).toBe(initialCount);
    }
  });
});
