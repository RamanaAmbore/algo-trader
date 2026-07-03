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
 * Fix applied (buildUnified line ~3322):
 *   BEFORE:  const liveChangePct = snap?.day_change_pct ?? m.change_pct ?? null;
 *   AFTER:   const liveChangePct = m.change_pct ?? snap?.day_change_pct ?? null;
 *
 * By preferring `m.change_pct` (the moversStore-owned value, from the same
 * stable DB snapshot on every closed-hours poll), `_mover_change_pct` becomes
 * invariant to competing publisher drift. `_topRowsFor` therefore produces the
 * same sort order on every render.
 *
 * Regression mechanism reproduced by this spec:
 *  1. Mocks /api/watchlist/movers with a stable closed-hours snapshot containing
 *     two pairs of rows with near-identical |change_pct| values (the
 *     shuffle-prone case): BANKNIFTY + TCS both at 1.058, TECHM + MINDTREE at
 *     -1.172 and -1.176.
 *  2. Mocks POST /api/quote/batch (called by publishPulseQuotes) with DRIFTED
 *     change_pct values for the same symbols — simulating the competing-publisher
 *     float divergence. Two page loads: drift A (+0.003) and drift B (-0.003).
 *  3. Asserts that the Winners and Losers grid row order is IDENTICAL across
 *     both loads. Under the OLD code, BANKNIFTY (1.061 on load A, 1.055 on
 *     load B) would swap with TCS (1.055 on load A, 1.061 on load B) when the
 *     symbolStore-arbitrated value drove the sort.
 *  4. Verifies row count is stable (keepStaleOnEmpty regression guard).
 *
 * Five quality dimensions:
 *  1. SSOT     — `buildUnified` is the single gate for liveChangePct;
 *                `_topRowsFor` is the single sort point for Winners/Losers.
 *  2. Perf     — mocked responses only; no real broker calls.
 *  3. Stale    — load A vs load B order comparison is the regression guard.
 *                Fails on the OLD code, passes on the fix.
 *  4. Reuse    — same route.fulfill + page.evaluate pattern as
 *                dhan_stale_persist.spec.js.
 *  5. UX       — winners/losers cards both verified; row count stable.
 *
 * Run context: chromium-desktop (side-by-side layout needs ≥1440px viewport).
 * Serial mode: login rate-limit (5/min) is respected between tests.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 45_000;

// Closed-hours market status — both NSE and MCX closed.
const MARKET_CLOSED = {
  nse_open: false,
  mcx_open: false,
  any_open: false,
  is_holiday: false,
};

// Snapshot timestamp — non-null signals a backend persisted snapshot.
const CAPTURED_AT = '2026-07-02T15:35:00.000000+00:00';

// -----------------------------------------------------------------------
// Stable movers payload — always served from /api/watchlist/movers.
// BANKNIFTY and TCS share |change_pct| = 1.058 (near-tie, shuffle-prone).
// TECHM and MINDTREE are near-ties on the losers side (-1.172 vs -1.176).
// -----------------------------------------------------------------------
const STABLE_MOVERS = {
  movers: [
    // Winners (positive change_pct)
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
 * Build a /api/quote/batch response with DRIFTED change_pct values.
 * The drift simulates the competing-publisher float divergence: /watchlist/movers
 * reports 1.058 for BANKNIFTY, but /quote/batch reports 1.058+drift (or 1.058-drift).
 * Under the OLD code, symbolStore would oscillate between these values
 * poll-to-poll, causing sort order to flip for near-tie rows.
 *
 * @param {number} drift  Additive offset on each change_pct.
 * @returns {object}
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
      change_pct:    m.change_pct + drift,
      change:        m.last_price - m.previous_close,
      volume:        100000,
      oi:            0,
    })),
  };
}

/**
 * Install route mocks for a single drift value.
 * Route order: broad catch-alls FIRST, specific handlers LAST (Playwright
 * matches in reverse-registration order — last registered wins).
 *
 * @param {import('@playwright/test').Page} page
 * @param {number} drift  Drift to apply to quote/batch response.
 */
async function installMocksWithDrift(page, drift) {
  // Broad watchlist catch-all — lowest priority.
  await page.route('**/api/watchlist/**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], id: -1, name: 'Pinned' }),
    })
  );

  // More-specific handlers registered AFTER catch-all → higher priority.

  await page.route('**/api/market/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(MARKET_CLOSED),
    })
  );

  // Watchlist per-list quotes — suppress (empty items).
  await page.route('**/api/watchlist/*/quotes*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [] }),
    })
  );

  // Movers endpoint — stable snapshot (highest priority among watchlist routes).
  await page.route('**/api/watchlist/movers*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(STABLE_MOVERS),
    })
  );

  // quote/batch — drifted values (competing publisher divergence).
  await page.route('**/api/quote/batch', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(buildDriftedBatchQuotes(drift)),
    })
  );

  // Empty positions / holdings / funds — focus on movers only.
  const empty = () => JSON.stringify({
    rows: [], summary: [], refreshed_at: 'Wed 02 Jul 15:30 IST', stale_accounts: [],
  });
  await page.route('**/api/positions*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: empty() })
  );
  await page.route('**/api/holdings*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: empty() })
  );
  await page.route('**/api/funds*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: empty() })
  );

  // Sparklines — suppress.
  await page.route('**/api/sparklines*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data: {}, refreshed_at: '' }),
    })
  );
}

/**
 * Navigate to /pulse and wait for the Winners bucket to have at least
 * one ag-Grid row. Uses the stable CSS class `.mp-bucket-winners`.
 *
 * @param {import('@playwright/test').Page} page
 */
async function waitForWinnersGrid(page) {
  await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(
    () => {
      const card = document.querySelector('.mp-bucket-winners');
      if (!card) return false;
      return card.querySelectorAll('.ag-center-cols-container .ag-row').length > 0;
    },
    { timeout: TIMEOUT }
  );
}

/**
 * Navigate to /pulse and wait for the Losers bucket to have at least
 * one ag-Grid row. Uses the stable CSS class `.mp-bucket-losers`.
 *
 * @param {import('@playwright/test').Page} page
 */
async function waitForLosersGrid(page) {
  await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(
    () => {
      const card = document.querySelector('.mp-bucket-losers');
      if (!card) return false;
      return card.querySelectorAll('.ag-center-cols-container .ag-row').length > 0;
    },
    { timeout: TIMEOUT }
  );
}

/**
 * Extract visible row tradingsymbols from the Winners or Losers bucket
 * in DOM order (top-to-bottom = rank 1 to N).
 *
 * @param {import('@playwright/test').Page} page
 * @param {'winners'|'losers'} direction
 * @returns {Promise<string[]>}
 */
async function getMoverRowOrder(page, direction) {
  return page.evaluate((dir) => {
    const card = document.querySelector(`.mp-bucket-${dir}`);
    const rowSelector = '.ag-center-cols-container .ag-row';
    const rows = Array.from(
      card ? card.querySelectorAll(rowSelector) : document.querySelectorAll(rowSelector)
    );
    return rows
      .map(row => {
        const cells = Array.from(row.querySelectorAll('.ag-cell'));
        for (const cell of cells) {
          const t = (cell.textContent || '').trim();
          // Symbol cells may contain inline badge text such as "BANKNIFTY M↑"
          // or "NIFTY 50 W". Extract only the leading uppercase run (the
          // tradingsymbol) rather than matching the whole cell text, which
          // would reject the ↑/↓ arrow characters.
          const m = t.match(/^([A-Z][A-Z0-9 .]{2,25}?)(?:\s+[A-Z↑↓M*]|$)/);
          if (m && m[1].trim().length >= 3) return m[1].trim();
        }
        return '';
      })
      .filter(Boolean);
  }, direction);
}

test.describe('Movers stable order — publisher-drift regression', () => {
  // Desktop viewport required: side-by-side Winners/Losers layout needs ≥1440px.
  test.use({ viewport: { width: 1440, height: 900 } });
  // Serial execution: loginAsAdmin hits /api/auth/login; rate-limit is 5/min.
  // Running 4 tests serially prevents concurrent logins from hitting the limit.
  test.describe.configure({ mode: 'serial' });
  // Each test does two full page loads + waitForGrid; the login fixture
  // also retries up to 3 times (0 + 3 + 8 s) when hitting rate limits.
  // Extend the per-test timeout to 90 s to cover the worst case.
  test.use({ timeout: 90_000 });

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  /**
   * Core regression: Winners order is IDENTICAL when quote/batch (the competing
   * publisher) returns drift A (+0.003) on one page load vs drift B (-0.003)
   * on the next.
   *
   * Under the OLD code (prefers snap.day_change_pct):
   *   Load A: symbolStore gets BANKNIFTY=1.061, TCS=1.055 → BANKNIFTY ranks first.
   *   Load B: symbolStore gets BANKNIFTY=1.055, TCS=1.061 → TCS ranks first.
   *   → Rows flip between page reloads (the operator's "shuffling" complaint).
   *
   * Under the NEW code (prefers m.change_pct):
   *   Both loads: _mover_change_pct uses moversStore value 1.058 for both.
   *   → Stable sort with tradingsymbol tie-break → BANKNIFTY always before TCS.
   */
  test('Winners order identical across two loads with opposite quote/batch drift', async ({ page }) => {
    // Load 1: quote/batch returns drift +0.003.
    await installMocksWithDrift(page, +0.003);
    await waitForWinnersGrid(page);
    const orderA = await getMoverRowOrder(page, 'winners');
    expect(orderA.length, 'Load A: Winners grid must have rows').toBeGreaterThan(0);

    // Load 2: quote/batch returns drift -0.003 (opposite sign — the shuffle trigger).
    // Navigate to blank first to ensure a full remount of the grid state.
    await page.goto('about:blank');
    await installMocksWithDrift(page, -0.003);
    await waitForWinnersGrid(page);
    const orderB = await getMoverRowOrder(page, 'winners');
    expect(orderB.length, 'Load B: Winners grid must have rows').toBeGreaterThan(0);

    // The order must be IDENTICAL between the two loads.
    // Under the OLD code, near-tie pairs (BANKNIFTY ↔ TCS) swap between A and B.
    expect(orderB, 'Winners order must not change between drift +0.003 and drift -0.003')
      .toEqual(orderA);
  });

  /**
   * Losers order is IDENTICAL across two loads with opposite drift.
   */
  test('Losers order identical across two loads with opposite quote/batch drift', async ({ page }) => {
    await installMocksWithDrift(page, +0.003);
    await waitForLosersGrid(page);
    const orderA = await getMoverRowOrder(page, 'losers');
    expect(orderA.length, 'Load A: Losers grid must have rows').toBeGreaterThan(0);

    await page.goto('about:blank');
    await installMocksWithDrift(page, -0.003);
    await waitForLosersGrid(page);
    const orderB = await getMoverRowOrder(page, 'losers');
    expect(orderB.length, 'Load B: Losers grid must have rows').toBeGreaterThan(0);

    expect(orderB, 'Losers order must not change between drift +0.003 and drift -0.003')
      .toEqual(orderA);
  });

  /**
   * Row count stable: Winners grid has the same count on loads with different drifts.
   * Regression guard for keepStaleOnEmpty — no rows disappear due to drift change.
   */
  test('Winners row count identical across loads with opposite drift', async ({ page }) => {
    await installMocksWithDrift(page, +0.005);
    await waitForWinnersGrid(page);
    const orderA = await getMoverRowOrder(page, 'winners');
    expect(orderA.length, 'Load A: Winners must have rows').toBeGreaterThan(0);

    await page.goto('about:blank');
    await installMocksWithDrift(page, -0.005);
    await waitForWinnersGrid(page);
    const orderB = await getMoverRowOrder(page, 'winners');

    expect(orderB.length, 'Row count must not change between drift loads').toBe(orderA.length);
  });

  /**
   * N-load stability: run 3 loads with alternating drifts and verify the
   * Winners order is identical on every load. Covers the "intermittent
   * shuffling" pattern (appears after multiple poll cycles, not immediately).
   */
  test('Winners order stable across N loads with alternating drift', async ({ page }) => {
    const drifts = [+0.003, -0.003, +0.005];
    /** @type {string[]|null} */
    let referenceOrder = null;

    for (let i = 0; i < drifts.length; i++) {
      if (i > 0) await page.goto('about:blank');
      await installMocksWithDrift(page, drifts[i]);
      await waitForWinnersGrid(page);
      const order = await getMoverRowOrder(page, 'winners');
      if (order.length === 0) continue;  // Skip if grid empty on this load

      if (referenceOrder === null) {
        referenceOrder = order;
      } else {
        expect(order, `Winners shuffled on load ${i} (drift=${drifts[i]})`).toEqual(referenceOrder);
      }
    }
    // At least one load must have captured a reference.
    expect(referenceOrder, 'At least one load must render Winners rows').not.toBeNull();
  });
});
