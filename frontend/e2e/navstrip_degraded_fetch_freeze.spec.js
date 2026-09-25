/**
 * navstrip_degraded_fetch_freeze.spec.js
 *
 * Real-money regression spec (2026-09) — "0 instead of last-known-good".
 *
 * Root cause (see .claude/PLAN.md §A3): when a positions fetch degrades
 * (conn-service masks a broker failure as a fake-fresh empty-200), the
 * backend now tags the response `stale_accounts: [...]` (PositionsResponse
 * — backend/api/schemas.py). This spec mocks that exact shape (empty rows
 * + a non-empty stale_accounts list) on the SECOND /api/positions poll
 * and asserts:
 *   1. NavStrip's P lifetime P&L pill keeps showing the last non-zero
 *      value from the first (healthy) poll — does NOT flash to 0.
 *   2. The strip picks up the `.ps-stale` staleness tint once the
 *      degraded response lands.
 *
 * Frontend fix under test: marketDataStores.svelte.js's positionsStore
 * `meta: _bookStaleMeta` + dataStore.svelte.js's createDataStore degraded
 * guard (drops an empty+degraded write, keeps the prior non-empty value) —
 * see dataStore.test.js for the unit-level coverage of the same guard.
 *
 * Five quality dimensions:
 *  1. SSOT   — exercises the real positionsStore singleton PositionStrip,
 *              PerformancePage, NavBreakdown, dashboard, and derivatives
 *              all read from — not a page-local mock
 *  2. Perf   — no extra network beyond the mocked routes; waits exactly one
 *              book-poller cycle (~5s foreground cadence)
 *  3. Stale  — this IS the stale-data test; also asserts the .ps-stale
 *              visual convention (existing amber-tint pattern, not a new one)
 *  4. Reuse  — mirrors closed_hours_day_change.spec.js's route-mock pattern
 *  5. UX     — the exact operator-visible symptom (P pill collapsing to 0)
 *              is what's asserted against, via the rendered DOM text
 *
 * Run:
 *   cd frontend && npx playwright test navstrip_degraded_fetch_freeze --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;

// A single healthy position with a real, non-zero lifetime P&L (`pnl`).
// portfolioStore.svelte.js's _livePositionsPnl (P pill slot 2) sums
// `r.pnl` directly with no baseline-diff/formatting ambiguity — the most
// deterministic slot to assert a frozen value against.
const HEALTHY_POSITION = {
  account: 'ZG0790',
  tradingsymbol: 'NIFTY25JANFUT',
  exchange: 'NFO',
  product: 'NRML',
  quantity: 50,
  average_price: 22800.0,
  last_price: 23050.0,
  close_price: 22900.0,
  prev_close: 22900.0,
  overnight_quantity: 50,
  pnl: 12500.0,
  realised: 0,
  unrealised: 12500.0,
  day_change_val: 12500.0,
  day_change_percentage: 1.09,
};

/**
 * Install the sequential positions mock: first call returns a healthy
 * non-empty response; every call after `degradeAfter` returns the exact
 * degraded shape this fix targets (empty rows + non-empty stale_accounts,
 * as_of null — a mid-session masked failure, NOT a closed-hours snapshot).
 *
 * Holdings is mocked to a stable, ALWAYS-healthy response with `as_of:
 * null` throughout — this keeps the book poller on its fast 5s foreground
 * cadence (marketDataStores.svelte.js's `_tickBookPollers` switches to a
 * 30-min cadence whenever `_holdingsSnapshotAt` — sourced from holdings'
 * `as_of` — is non-null; a non-null as_of here would starve this test of
 * the second poll it needs).
 *
 * @param {import('@playwright/test').Page} page
 * @param {{ degradeAfter: number }} opts
 */
async function installDegradedPositionsMock(page, { degradeAfter }) {
  let callCount = 0;

  // NOTE: Playwright glob `*` does not match `/` — fetchPositions() calls
  // `/api/positions/` (trailing slash, no query params when fresh/skipLtp
  // are both false), so the pattern must be `/**` (double-star matches the
  // trailing slash, including zero further characters), not a trailing `*`.
  await page.route('**/api/positions/**', (route) => {
    callCount++;
    const degraded = callCount > degradeAfter;
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(
        degraded
          ? {
              // The exact masked-failure shape: broker call failed, the
              // conn-service/route layer returned a fake-fresh empty 200,
              // but the backend fix (A1/A2) tags it via stale_accounts so
              // the frontend can tell this apart from a genuinely empty book.
              rows: [],
              summary: [],
              refreshed_at: 'degraded-poll',
              as_of: null,
              stale_accounts: ['ZG0790'],
            }
          : {
              rows: [HEALTHY_POSITION],
              summary: [
                { account: 'ZG0790', pnl: 12500.0, day_change_val: 12500.0, day_change_percentage: 1.09, day_prev_val: 1145000.0 },
                { account: 'TOTAL', pnl: 12500.0, day_change_val: 12500.0, day_change_percentage: 1.09, day_prev_val: 1145000.0 },
              ],
              refreshed_at: 'healthy-poll',
              as_of: null,
              stale_accounts: [],
            }
      ),
    });
  });

  await page.route('**/api/holdings/**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        rows: [],
        summary: [],
        refreshed_at: 'healthy-poll',
        as_of: null, // keep the book poller on the fast 5s cadence — see doc above
        stale_accounts: [],
      }),
    })
  );
}

test.describe('NavStrip — degraded positions fetch freezes at last-known-good', () => {
  test.setTimeout(60_000);

  test('P lifetime pill keeps its last non-zero value and .ps-stale appears when a poll comes back degraded', async ({ page }) => {
    await loginAsAdmin(page);
    await installDegradedPositionsMock(page, { degradeAfter: 1 });

    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const strip = page.locator('.ps-strip').first();
    await expect(strip).toBeVisible({ timeout: TIMEOUT });

    // P pill group is the first .ps-agg; slot index 1 (0-based) within it
    // is _livePositionsPnl (lifetime P&L, Σ r.pnl — no baseline-diff math,
    // the most deterministic slot to assert a frozen value against).
    const pLifetimeCell = strip.locator('.ps-agg').first().locator('.ps-agg-v').nth(1);

    // Wait for the first (healthy) poll to land and paint the real value.
    // aggCompact(12500) → "13K" (Math.round(12500/1000)) — distinct from
    // the placeholder zero-render "0.00" (_decFmt(0) via aggCompact's
    // <1000 branch), so this also guards against a false-pass on the
    // pre-poll placeholder text.
    await expect(pLifetimeCell).toHaveText('13K', { timeout: TIMEOUT });
    const healthyText = (await pLifetimeCell.innerText()).trim();
    console.log('[navstrip_degraded_fetch_freeze] healthy poll P lifetime value:', healthyText);

    // Wait long enough for the book poller's next cycle (5s foreground
    // cadence) to fire and land the degraded (empty + stale_accounts) response.
    await page.waitForTimeout(7_000);

    // THE FIX: the P lifetime pill must still show the SAME value — not 0,
    // not blank. Before the fix, positionsStore.value would have been
    // overwritten with `[]`, and _livePositionsPnl would recompute to 0.
    await expect(pLifetimeCell).toHaveText(healthyText, { timeout: TIMEOUT });

    // Staleness indicator: the strip now carries the existing `.ps-stale`
    // amber-tint convention, driven by positionsStore.meta.degraded — not
    // a newly-invented visual pattern (see PositionStrip.svelte's _anyDegraded).
    await expect(strip).toHaveClass(/ps-stale/, { timeout: TIMEOUT });

    console.log('[navstrip_degraded_fetch_freeze] value held frozen through degraded poll:', healthyText);
  });
});
