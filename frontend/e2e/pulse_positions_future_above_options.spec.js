/**
 * pulse_positions_future_above_options.spec.js
 *
 * 2026-10-17 same-day follow-up to pulse_positions_underlying_block_order.spec.js
 * — that fix protected CROSS-block order (one underlying's whole block
 * staying together), but a SEPARATE bug survived one level deeper:
 * WITHIN a single underlying+account block, `_pairGroupPostSort`
 * (MarketPulse.svelte) re-sorts via `pairGroupSort()`
 * (pairGroupSort.js), whose key was `${account}|${pair_group_key ??
 * 'ZZZZ_orphan'}` — no tier awareness. A lone future/underlying row
 * (which has no pair_group_key, since it can only waterfall-pair
 * against another future) fell into the 'ZZZZ_orphan' bucket and
 * sorted to the END of its account's cluster — after P1's two option
 * legs AND the extra orphan option — instead of the START. This is the
 * exact "gold is showing below, it should be the first one as an
 * underlying" symptom, reproduced here with a future + a P1 pair + an
 * orphan option, all in ONE account, so the bug (which only manifests
 * once 2+ distinct pair-groups/orphans exist in a block) is actually
 * exercised.
 *
 * Fix: pairGroupSort.js's shared key gained a tier segment
 * (`pairGroupOrderKey()`) — underlying/future is tier 0, options tier 1
 * — inserted between account and pair_group_key.
 *
 * Run:
 *   npx playwright test e2e/pulse_positions_future_above_options.spec.js \
 *   --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;
const POSITIONS_RE = /\/api\/positions\/(\?.*)?$/;
const ACCT = 'ZG0790';

// One account, one underlying (GOLDM) — a future (orphan, no pair_group_key),
// a P1 pair (two option legs), and an extra orphan option leg. Backend
// auto-pairing is bypassed here — pair_group_key/is_orphan are supplied
// directly in the mocked response, same convention as
// position_state_label_info_tooltip.spec.js.
const ROWS = [
  {
    tradingsymbol: 'GOLDM26DEC77000CE', symbol: 'GOLDM26DEC77000CE', exchange: 'MCX',
    account: ACCT, product: 'NRML', quantity: 1, average_price: 300, last_price: 320,
    pnl: 20, realised: 0, unrealised: 20, day_pnl: 20, prev_settlement_pnl: 0, close_price: 300,
    pair_group_key: null, is_orphan: true,
  },
  {
    tradingsymbol: 'GOLDM26DEC76000PE', symbol: 'GOLDM26DEC76000PE', exchange: 'MCX',
    account: ACCT, product: 'NRML', quantity: -1, average_price: 150, last_price: 130,
    pnl: 20, realised: 0, unrealised: 20, day_pnl: 20, prev_settlement_pnl: 0, close_price: 150,
    pair_group_key: 'P1', is_orphan: false,
  },
  {
    tradingsymbol: 'GOLDM26DECFUT', symbol: 'GOLDM26DECFUT', exchange: 'MCX',
    account: ACCT, product: 'NRML', quantity: 10, average_price: 75700, last_price: 75750,
    pnl: 500, realised: 0, unrealised: 500, day_pnl: 500, prev_settlement_pnl: 0, close_price: 75700,
    pair_group_key: null, is_orphan: true,
  },
  {
    tradingsymbol: 'GOLDM26DEC75000CE', symbol: 'GOLDM26DEC75000CE', exchange: 'MCX',
    account: ACCT, product: 'NRML', quantity: 1, average_price: 1200, last_price: 1250,
    pnl: 50, realised: 0, unrealised: 50, day_pnl: 50, prev_settlement_pnl: 0, close_price: 1200,
    pair_group_key: 'P1', is_orphan: false,
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

test.describe('MarketPulse Positions grid — underlying/future sorts above its own options within a block', () => {
  test('the future row renders above BOTH the P1 pair legs and the orphan option, in the same account', async ({ page }) => {
    await loginAsAdmin(page);
    await mockPositions(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const futRow = page.locator(`[row-id="GOLDM26DECFUT::${ACCT}__pos"]`).first();
    const p1aRow = page.locator(`[row-id="GOLDM26DEC75000CE::${ACCT}__pos"]`).first();
    const p1bRow = page.locator(`[row-id="GOLDM26DEC76000PE::${ACCT}__pos"]`).first();
    const orphanRow = page.locator(`[row-id="GOLDM26DEC77000CE::${ACCT}__pos"]`).first();

    await expect(futRow).toBeVisible({ timeout: TIMEOUT });
    await expect(p1aRow).toBeVisible({ timeout: TIMEOUT });
    await expect(p1bRow).toBeVisible({ timeout: TIMEOUT });
    await expect(orphanRow).toBeVisible({ timeout: TIMEOUT });

    const [futBox, p1aBox, p1bBox, orphanBox] = await Promise.all([
      futRow.boundingBox(), p1aRow.boundingBox(), p1bRow.boundingBox(), orphanRow.boundingBox(),
    ]);

    expect(futBox.y, 'future must render above the P1 pair\'s first leg')
      .toBeLessThan(p1aBox.y);
    expect(futBox.y, 'future must render above the P1 pair\'s second leg')
      .toBeLessThan(p1bBox.y);
    expect(futBox.y, 'future must render above the orphan option leg')
      .toBeLessThan(orphanBox.y);
  });
});
