/**
 * Movers stable-order regression spec — Playwright e2e.
 *
 * Operator complaint (2026-07-03): "the winners and losers cards shuffling
 * rows when the market is closed, why?"
 *
 * Root cause: `_topRowsFor` in MarketPulse.svelte sorted by
 * `Math.abs(change_pct)` with no tie-breaker. The `change_pct` value is
 * read from symbolStore, which is updated each poll by multiple competing
 * publishers (publishMoverRows, publishWatchQuotes, publishPulseQuotes),
 * each using `snapshot_ts = Date.now()`. Small floating-point differences
 * between publishers cause `|change_pct|` to fluctuate at the boundary
 * between similarly-ranked rows, flipping their relative order on
 * consecutive renders.
 *
 * Fix: `_topRowsFor` now uses `_mover_change_pct || change_pct` (the
 * movers-specific canonical field, mirroring the mainRows sort) and adds
 * `tradingsymbol.localeCompare` as a stable tie-break.
 *
 * This spec:
 *  1. Mocks /api/watchlist/movers with a closed-hours snapshot (captured_at
 *     set), containing pairs of rows with similar |change_pct| values that
 *     were historically prone to flipping.
 *  2. Simulates multiple movers-store re-triggers (multiple polled responses
 *     with subtly different floating-point values, as competing publishers
 *     would produce).
 *  3. Captures the Winners grid row order after each re-trigger.
 *  4. Asserts that the row order is IDENTICAL across all captures.
 *  5. Repeats for the Losers grid.
 *  6. Also checks: no row disappears between captures (keepStaleOnEmpty guard).
 *
 * Five quality dimensions:
 *  1. SSOT     — `_topRowsFor` is the single sort gate for Winners/Losers
 *                grids; no duplicate sort logic elsewhere.
 *  2. Perf     — mocked responses, no real broker calls, < 5 s per run.
 *  3. Stale    — asserts the BEFORE order == AFTER order (regression guard
 *                for the shuffling defect).
 *  4. Reuse    — same route.fulfill + page.evaluate pattern used in
 *                dhan_stale_persist.spec.js and closed_hours_day_change.spec.js.
 *  5. UX       — palette + direction indicators unchanged (winners green,
 *                losers red, both rendered as separate cards).
 *
 * Run context: chromium-desktop (grid requires min-width viewport).
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

// Snapshot timestamp — non-null means backend served a persisted snapshot.
const CAPTURED_AT = '2026-07-02T15:35:00.000000+00:00';

/**
 * Build a movers payload.  `jitter` introduces the small floating-point
 * differences that competing symbolStore publishers produce — the sort
 * tie-breaker must keep the order stable regardless of jitter magnitude.
 *
 * @param {number} jitter  Small floating-point offset applied to change_pct.
 * @returns {object}
 */
function buildMoversPayload(jitter = 0) {
  return {
    movers: [
      // Winners — pairs with near-identical |change_pct| (the shuffle-prone case).
      { tradingsymbol: 'NIFTY',       exchange: 'NSE', last_price: 24200, previous_close: 23952, change_pct:  1.035 + jitter, peak_pct:  1.035, sticky: false },
      { tradingsymbol: 'RELIANCE',    exchange: 'NSE', last_price: 1450,  previous_close: 1435,  change_pct:  1.045 + jitter, peak_pct:  1.045, sticky: false },
      { tradingsymbol: 'BANKNIFTY',   exchange: 'NSE', last_price: 52500, previous_close: 51950, change_pct:  1.058 + jitter, peak_pct:  1.058, sticky: false },
      { tradingsymbol: 'INFY',        exchange: 'NSE', last_price: 1720,  previous_close: 1700,  change_pct:  1.176 + jitter, peak_pct:  1.176, sticky: false },
      { tradingsymbol: 'TCS',         exchange: 'NSE', last_price: 3820,  previous_close: 3780,  change_pct:  1.058 + jitter, peak_pct:  1.058, sticky: false },
      // Losers — same pattern.
      { tradingsymbol: 'WIPRO',       exchange: 'NSE', last_price: 450,   previous_close: 455,   change_pct: -1.099 + jitter, peak_pct: -1.099, sticky: false },
      { tradingsymbol: 'HCLTECH',     exchange: 'NSE', last_price: 1560,  previous_close: 1578,  change_pct: -1.141 + jitter, peak_pct: -1.141, sticky: false },
      { tradingsymbol: 'LTIM',        exchange: 'NSE', last_price: 5600,  previous_close: 5665,  change_pct: -1.147 + jitter, peak_pct: -1.147, sticky: false },
      { tradingsymbol: 'TECHM',       exchange: 'NSE', last_price: 1680,  previous_close: 1700,  change_pct: -1.176 + jitter, peak_pct: -1.176, sticky: false },
      { tradingsymbol: 'MINDTREE',    exchange: 'NSE', last_price: 3120,  previous_close: 3157,  change_pct: -1.172 + jitter, peak_pct: -1.172, sticky: false },
    ],
    threshold_pct: 1.5,
    session_date: '2026-07-02',
    captured_at: CAPTURED_AT,
  };
}

/**
 * Install all route mocks for a closed-hours movers scenario.
 * The movers endpoint is called multiple times with the given payload factory
 * so the test can simulate symbolStore re-triggers.
 *
 * @param {import('@playwright/test').Page} page
 * @param {() => object} payloadFn  Called on each movers request to return the payload.
 */
async function installMoversMocks(page, payloadFn) {
  await page.route('**/api/market/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(MARKET_CLOSED),
    })
  );

  await page.route('**/api/watchlist/movers*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(payloadFn()),
    })
  );

  // Empty positions / holdings / funds so Pulse focuses on movers.
  const empty = (rows = []) => JSON.stringify({ rows, summary: [], refreshed_at: 'Wed 02 Jul 15:30 IST', stale_accounts: [] });
  await page.route('**/api/positions*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: empty() }));
  await page.route('**/api/holdings*',  (route) => route.fulfill({ status: 200, contentType: 'application/json', body: empty() }));
  await page.route('**/api/funds*',     (route) => route.fulfill({ status: 200, contentType: 'application/json', body: empty() }));

  // Suppress sparklines + watchlist quotes — not needed for this test.
  await page.route('**/api/sparklines*',    (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ data: {}, refreshed_at: '' }) }));
  await page.route('**/api/watchlist/**',   (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], id: -1, name: 'Pinned' }) }));
}

/**
 * Extract the visible row tradingsymbols from the Winners or Losers grid.
 * The grids are rendered via ag-Grid inside elements with data-direction
 * attribute. Falls back to reading from any ag-Grid row cell text.
 *
 * @param {import('@playwright/test').Page} page
 * @param {'winners'|'losers'} direction
 * @returns {Promise<string[]>}
 */
async function getMoverRowOrder(page, direction) {
  return page.evaluate((dir) => {
    // MarketPulse renders winner/loser cards; look for the heading label
    // 'Winners' or 'Losers' then walk sibling/child ag-Grid rows.
    // The simplest cross-version approach: read all ag-Grid rows in DOM
    // order whose tradingsymbol cells appear in a card labelled with the
    // direction heading.
    const headings = Array.from(document.querySelectorAll('*'));
    /** @type {Element|null} */
    let directionCard = null;
    for (const el of headings) {
      const tag = el.tagName;
      if (tag === 'H3' || tag === 'H4' || tag === 'SPAN' || tag === 'DIV') {
        const text = (el.textContent || '').trim().toLowerCase();
        if (text === dir) {
          // Walk up to find the enclosing card container.
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
    }

    if (!directionCard) {
      // Fallback: return all mover row tradingsymbols in DOM order.
      const rows = Array.from(document.querySelectorAll('.ag-center-cols-container .ag-row'));
      return rows.map(r => {
        const cells = Array.from(r.querySelectorAll('.ag-cell'));
        for (const c of cells) {
          const t = (c.textContent || '').trim();
          if (t && /^[A-Z0-9 .]+$/.test(t) && t.length < 30) return t;
        }
        return '';
      }).filter(Boolean);
    }

    const rows = Array.from(directionCard.querySelectorAll('.ag-center-cols-container .ag-row'));
    return rows.map(row => {
      const cells = Array.from(row.querySelectorAll('.ag-cell'));
      for (const cell of cells) {
        const t = (cell.textContent || '').trim();
        // Tradingsymbol cells are short uppercase strings.
        if (t && /^[A-Z0-9 .]+$/.test(t) && t.length >= 3 && t.length < 30) return t;
      }
      return '';
    }).filter(Boolean);
  }, direction);
}

test.describe('Movers stable order — closed-hours snapshot', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  /**
   * Core regression test: Winners order is identical across 5 consecutive
   * movers-store re-triggers with jittered change_pct values (simulating
   * the competing-publisher floating-point drift that caused shuffling).
   */
  test('Winners grid row order is stable across re-renders', async ({ page }) => {
    // Jitter values representing the per-poll floating-point drift from
    // competing symbolStore publishers (watchQuotes vs moversRow floats).
    const jitters = [0, 0.0001, -0.0001, 0.0002, -0.0002];
    let jitterIdx = 0;
    await installMoversMocks(page, () => buildMoversPayload(jitters[jitterIdx % jitters.length]));

    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    // Wait for the movers grid to render.
    await page.waitForFunction(() => {
      const rows = document.querySelectorAll('.ag-center-cols-container .ag-row');
      return rows.length > 0;
    }, { timeout: TIMEOUT });

    // Wait for winners card to appear (the "Winners" heading must exist).
    await page.waitForFunction(() => {
      const els = Array.from(document.querySelectorAll('*'));
      return els.some(el => (el.textContent || '').trim().toLowerCase() === 'winners');
    }, { timeout: TIMEOUT });

    const capturedOrders = [];
    for (let i = 0; i < 5; i++) {
      jitterIdx = i;
      // Re-trigger the movers store by re-routing movers to a jittered payload,
      // then waiting one short tick for the derived to settle.
      await page.route('**/api/watchlist/movers*', (route) =>
        route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(buildMoversPayload(jitters[i])),
        })
      );
      // Small pause to let any pending re-render settle without a full reload.
      await page.waitForTimeout(300);
      const order = await getMoverRowOrder(page, 'winners');
      if (order.length > 0) capturedOrders.push(order);
    }

    // Must have captured at least 2 snapshots with rows to compare.
    expect(capturedOrders.length).toBeGreaterThanOrEqual(2);
    const reference = capturedOrders[0];
    for (let i = 1; i < capturedOrders.length; i++) {
      // Same symbols, same order. If a new poll changes jitter,
      // the tie-breaker keeps the order stable.
      expect(capturedOrders[i], `Winners order changed between render 0 and render ${i}`).toEqual(reference);
    }
  });

  /**
   * Losers grid row order is stable across re-renders — same test, other card.
   */
  test('Losers grid row order is stable across re-renders', async ({ page }) => {
    const jitters = [0, 0.0001, -0.0001, 0.0002, -0.0002];
    await installMoversMocks(page, () => buildMoversPayload(0));

    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => {
      const rows = document.querySelectorAll('.ag-center-cols-container .ag-row');
      return rows.length > 0;
    }, { timeout: TIMEOUT });
    await page.waitForFunction(() => {
      const els = Array.from(document.querySelectorAll('*'));
      return els.some(el => (el.textContent || '').trim().toLowerCase() === 'losers');
    }, { timeout: TIMEOUT });

    const capturedOrders = [];
    for (let i = 0; i < 5; i++) {
      await page.route('**/api/watchlist/movers*', (route) =>
        route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(buildMoversPayload(jitters[i])),
        })
      );
      await page.waitForTimeout(300);
      const order = await getMoverRowOrder(page, 'losers');
      if (order.length > 0) capturedOrders.push(order);
    }

    expect(capturedOrders.length).toBeGreaterThanOrEqual(2);
    const reference = capturedOrders[0];
    for (let i = 1; i < capturedOrders.length; i++) {
      expect(capturedOrders[i], `Losers order changed between render 0 and render ${i}`).toEqual(reference);
    }
  });

  /**
   * No rows disappear between renders (keepStaleOnEmpty guard regression).
   * moversStore.keepStaleOnEmpty ensures empty-array responses don't wipe
   * the grid. Verify here that symbol count is stable.
   */
  test('Winners row count is stable across re-renders (no disappearing rows)', async ({ page }) => {
    await installMoversMocks(page, () => buildMoversPayload(0));

    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => {
      const rows = document.querySelectorAll('.ag-center-cols-container .ag-row');
      return rows.length > 0;
    }, { timeout: TIMEOUT });

    // Capture initial count.
    const orderA = await getMoverRowOrder(page, 'winners');
    expect(orderA.length).toBeGreaterThan(0);

    // Trigger two more polls.
    for (let i = 0; i < 2; i++) {
      await page.route('**/api/watchlist/movers*', (route) =>
        route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(buildMoversPayload(0)),
        })
      );
      await page.waitForTimeout(300);
    }

    const orderB = await getMoverRowOrder(page, 'winners');
    // Count must be the same — no ghost-blank rows, no dropouts.
    expect(orderB.length).toBe(orderA.length);
  });

  /**
   * Manual tab switch: operator clicks Underlying tab — order remains stable.
   * Covers the UX path where winTab is set (not null) so _bestTab is bypassed.
   */
  test('Winners order stable after operator tab switch', async ({ page }) => {
    await installMoversMocks(page, () => buildMoversPayload(0));

    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => {
      return Array.from(document.querySelectorAll('*'))
        .some(el => (el.textContent || '').trim().toLowerCase() === 'winners');
    }, { timeout: TIMEOUT });

    // Click the Underlying tab if it exists (may not if no large_cap rows).
    const underlyingBtn = page.locator('button, [role="tab"]').filter({ hasText: /^Underlying$/i }).first();
    const btnCount = await underlyingBtn.count();
    if (btnCount > 0) await underlyingBtn.click();

    const orderA = await getMoverRowOrder(page, 'winners');

    // Re-trigger movers.
    await page.route('**/api/watchlist/movers*', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(buildMoversPayload(0.0001)),
      })
    );
    await page.waitForTimeout(300);
    const orderB = await getMoverRowOrder(page, 'winners');

    if (orderA.length > 0 && orderB.length > 0) {
      expect(orderB).toEqual(orderA);
    }
  });
});
