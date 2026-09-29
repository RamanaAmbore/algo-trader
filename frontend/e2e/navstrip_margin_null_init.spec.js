/**
 * navstrip_margin_null_init.spec.js
 *
 * Real-money regression spec (2026-09) — NavStrip "₹0 margin for an
 * extended period, then jumps to the correct value" incident.
 *
 * Root cause (see .claude/PLAN.md): portfolioStore.svelte.js's
 * _marginAvail / _marginTotal / _liveCashTotal used to freeze the WHOLE
 * cross-account total at a remembered scalar that started at 0 on every
 * fresh page load. If the first poll(s) landed while `fundsStore.meta.
 * degraded` was true (or, as isolated here, before ANY funds poll has
 * ever resolved at all), the getters returned that frozen 0 forever —
 * the operator saw a flat ₹0 margin/cash instead of the honest "we don't
 * know yet" state.
 *
 * Fixed design: these three getters now return `null` (not 0) when no
 * funds poll has ever landed, and PositionStrip's `fmtMoney` renders
 * `null` as '—' (never '₹0' or 'NaN'). This spec isolates that exact
 * pre-first-poll window by mocking `/api/funds/` to hang indefinitely —
 * `fundsStore.value` stays `null` for the whole test, exactly matching
 * the "no poll has landed yet" case the fix targets.
 *
 * The second, more subtle assertion covers the null-init trap found
 * during design review: `cashTotal = liveCashTotal + longOptionsCashPaid`
 * — if `liveCashTotal` were still coerced to 0 (the old bug, or a
 * regression reintroducing implicit `null + x` arithmetic), `cashTotal`
 * would silently render `longOptionsCashPaid` ALONE as "cash" — a
 * subtly WRONG non-dash number, not an obviously-missing one. A position
 * carrying a real long CE premium (`longOptionsCashPaid > 0`) is mocked
 * specifically so this trap has something non-zero to leak.
 *
 * Five quality dimensions:
 *  1. SSOT   — exercises the real positionsStore/fundsStore singletons
 *              PositionStrip reads from, not a page-local mock.
 *  2. Perf   — single page load, no polling wait needed (funds never
 *              resolves, so there's nothing to wait for beyond mount).
 *  3. Stale  — this IS the "no poll landed yet" null-state test, the
 *              sibling of navstrip_degraded_fetch_freeze.spec.js's
 *              "degraded mid-session poll" case.
 *  4. Reuse  — mirrors navstrip_degraded_fetch_freeze.spec.js's
 *              route-mock + `.ps-strip` DOM-locator pattern.
 *  5. UX     — asserts the exact operator-visible symptom class (M/C
 *              pills reading '—' vs a wrong non-dash number).
 *
 * Run:
 *   cd frontend && npx playwright test navstrip_margin_null_init --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;

// Long CE option position — gives longOptionsCashPaid a real non-zero
// value (avg_price × qty fallback path, since instruments cache is cold
// in this test env) so the cashTotal null-init trap has something to leak.
const LONG_CE_POSITION = {
  account: 'ZG0790',
  tradingsymbol: 'NIFTY25JAN24000CE',
  exchange: 'NFO',
  product: 'NRML',
  quantity: 50,
  average_price: 120.5,
  last_price: 130.0,
  close_price: 118.0,
  prev_close: 118.0,
  overnight_quantity: 0,
  pnl: 475.0,
  realised: 0,
  unrealised: 475.0,
  day_change_val: 475.0,
  day_change_percentage: 3.2,
};

/**
 * @param {import('@playwright/test').Page} page
 */
async function installMocks(page) {
  // Positions: one healthy long-CE row (real, non-degraded response).
  await page.route('**/api/positions/**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        rows: [LONG_CE_POSITION],
        summary: [
          { account: 'ZG0790', pnl: 475.0, day_change_val: 475.0, day_change_percentage: 3.2, day_prev_val: 15_000.0 },
          { account: 'TOTAL', pnl: 475.0, day_change_val: 475.0, day_change_percentage: 3.2, day_prev_val: 15_000.0 },
        ],
        refreshed_at: 'healthy-poll',
        as_of: null,
        stale_accounts: [],
      }),
    })
  );

  // Holdings: empty but healthy, as_of:null keeps the book poller on its
  // fast foreground cadence (matches navstrip_degraded_fetch_freeze.spec.js).
  await page.route('**/api/holdings/**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ rows: [], summary: [], refreshed_at: 'healthy-poll', as_of: null, stale_accounts: [] }),
    })
  );

  // Funds: NEVER resolves. Isolates the "no funds poll has ever landed"
  // window — fundsStore.value stays null for the whole test, exactly the
  // state _marginAvail / _marginTotal / _liveCashTotal must render as
  // null (→ '—'), not 0.
  await page.route('**/api/funds/**', () => {
    // Intentionally never call route.fulfill()/route.continue() — the
    // request hangs until the test ends.
  });
}

test.describe('NavStrip — M/C pills render "—" (not ₹0 / NaN) before the first funds poll ever lands', () => {
  test.setTimeout(45_000);

  test('margin + cash pills show em-dash placeholders, and cashTotal does not silently leak longOptionsCashPaid alone', async ({ page }) => {
    await loginAsAdmin(page);

    // Clear the persistent (localStorage) cache tier BEFORE any app script
    // runs — otherwise fundsStore hydrates synchronously from a prior
    // test run's disk cache at module-eval time and the null-init path
    // under test never actually exercises (see persistentCache.js's
    // 'rbq.cache.' prefix / dataStore.svelte.js's _initFromCache()).
    await page.addInitScript(() => {
      try {
        for (let i = localStorage.length - 1; i >= 0; i--) {
          const k = localStorage.key(i);
          if (k && k.startsWith('rbq.cache.')) localStorage.removeItem(k);
        }
      } catch (_) { /* private mode / unavailable */ }
    });

    await installMocks(page);

    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const strip = page.locator('.ps-strip').first();
    await expect(strip).toBeVisible({ timeout: TIMEOUT });

    // Confirm the positions poll DID land (sanity check — proves the page
    // is actually live and reactive, not just frozen at initial markup).
    const pLifetimeCell = strip.locator('.ps-agg').first().locator('.ps-agg-v').nth(1);
    // aggCompact(475) → _decFmt(475) → |475| >= 100 → integer-collapse → "475".
    await expect(pLifetimeCell).toHaveText('475', { timeout: TIMEOUT });

    // M pill = 2nd .ps-agg (Available / Total margin).
    const mPill = strip.locator('.ps-agg').nth(1);
    const mAvail = mPill.locator('.ps-agg-v').nth(0);
    const mTotal = mPill.locator('.ps-agg-v').nth(1);
    await expect(mAvail).toHaveText('—', { timeout: TIMEOUT });
    await expect(mTotal).toHaveText('—', { timeout: TIMEOUT });

    // C pill = 3rd .ps-agg (Live Cash / Total Cash incl. long-option premium).
    const cPill = strip.locator('.ps-agg').nth(2);
    const cLive  = cPill.locator('.ps-agg-v').nth(0);
    const cTotal = cPill.locator('.ps-agg-v').nth(1);
    await expect(cLive).toHaveText('—', { timeout: TIMEOUT });
    // THE NULL-INIT TRAP: before the fix, `liveCashTotal + longOptionsCashPaid`
    // with liveCashTotal coerced to 0 would render longOptionsCashPaid ALONE
    // here (avg 120.5 × qty 50 = 6025 → aggCompact → "6K") instead of '—'.
    await expect(cTotal).toHaveText('—', { timeout: TIMEOUT });
    const cTotalText = (await cTotal.innerText()).trim();
    expect(cTotalText, 'cashTotal must never silently render longOptionsCashPaid alone when liveCashTotal is null')
      .not.toBe('6K');
  });
});
