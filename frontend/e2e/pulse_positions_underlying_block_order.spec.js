/**
 * pulse_positions_underlying_block_order.spec.js
 *
 * Regression guard for a 2026-10-17 operator-reported bug: "gold is
 * showing below, it should be the first one as an underlying." Root
 * cause: MarketPulse's Positions grid `postSortRows` callback
 * (`_pairGroupPostSort` in MarketPulse.svelte) called bare
 * `pairGroupSort(params.nodes)`, which re-sorts the WHOLE row set by
 * `${account}|${pair_group_key}` with no regard for `underlying` —
 * discarding the underlying-block order `sortUnifiedRows`
 * (pulseUnified.js) already established upstream. An underlying whose
 * account/pair label happened to sort alphabetically EARLY could jump
 * ahead of an underlying that should have stayed first, purely by
 * account-string coincidence — exactly the GOLD symptom reported.
 *
 * Fixed by composing underlying-block preservation (same technique
 * `postSortGroups`, every OTHER bucket grid's default, already uses)
 * with the account-then-pair ordering WITHIN each underlying's own
 * block only.
 *
 * Fixture: CRUDEOIL (account "ZZZZ9999", alphabetically LATE) and
 * NIFTY (account "AAAA0001", alphabetically EARLY) — CRUDEOIL sorts
 * first as an underlying by plain alphabetical fallback (no operator
 * pin needed, C < N); NIFTY's account would incorrectly jump it to
 * the top under the pre-fix bug, since "AAAA0001" < "ZZZZ9999".
 *
 * Run:
 *   npx playwright test e2e/pulse_positions_underlying_block_order.spec.js \
 *   --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;
const POSITIONS_RE = /\/api\/positions\/(\?.*)?$/;

const ROWS = [
  {
    tradingsymbol: 'CRUDEOIL26OCTFUT', symbol: 'CRUDEOIL26OCTFUT', exchange: 'MCX',
    account: 'ZZZZ9999', product: 'NRML', quantity: 10, average_price: 5700, last_price: 5750,
    pnl: 500, realised: 0, unrealised: 500, day_pnl: 100, prev_settlement_pnl: 0, close_price: 5700,
  },
  {
    tradingsymbol: 'NIFTY26JUNFUT', symbol: 'NIFTY26JUNFUT', exchange: 'NFO',
    account: 'AAAA0001', product: 'NRML', quantity: 50, average_price: 24000, last_price: 24100,
    pnl: 5000, realised: 0, unrealised: 5000, day_pnl: 1000, prev_settlement_pnl: 0, close_price: 24000,
  },
];

async function mockPositions(page) {
  await page.route(POSITIONS_RE, async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    let body = { rows: [] };
    try {
      const real = await route.fetch();
      body = { ...(await real.json()) };
    } catch (_) { /* backend unreachable — minimal envelope below */ }
    body.rows = ROWS;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

test.describe('MarketPulse Positions grid — underlying-block order survives account/pair post-sort', () => {
  test('CRUDEOIL (alphabetically-first underlying) stays first even though NIFTY has the alphabetically-earlier account', async ({ page }) => {
    await loginAsAdmin(page);
    await mockPositions(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    // Exact row-id, not a bare substring match — `*=` could also match an
    // unrelated bucket's row for the same symbol (e.g. watchlist/movers),
    // which sits at a totally different position on the page and isn't
    // what this test is about.
    const crudeRow = page.locator('[row-id="CRUDEOIL26OCTFUT::ZZZZ9999__pos"]').first();
    const niftyRow = page.locator('[row-id="NIFTY26JUNFUT::AAAA0001__pos"]').first();
    await expect(crudeRow).toBeVisible({ timeout: TIMEOUT });
    await expect(niftyRow).toBeVisible({ timeout: TIMEOUT });

    const [crudeBox, niftyBox] = await Promise.all([crudeRow.boundingBox(), niftyRow.boundingBox()]);
    expect(crudeBox, 'CRUDEOIL row not found').not.toBeNull();
    expect(niftyBox, 'NIFTY row not found').not.toBeNull();
    expect(crudeBox.y, 'CRUDEOIL (underlying-first) must render above NIFTY despite its alphabetically-later account')
      .toBeLessThan(niftyBox.y);
  });
});
