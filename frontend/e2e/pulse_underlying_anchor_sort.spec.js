/**
 * pulse_underlying_anchor_sort.spec.js
 *
 * Regression guard for the 2026-10-18 MarketPulse underlying-anchor-row
 * sort fix. Operator-reported ("GOLD at the bottom, should be first") and
 * screenshot-confirmed: `mergeUnderlyingAnchors`'s synthetic underlying row
 * (e.g. "GOLD" shown for a held GOLD option position, even though GOLD
 * itself isn't held) rendered at the BOTTOM of its own underlying's block
 * in the Positions grid instead of leading it.
 *
 * Root cause (confirmed via `pulseGridSetup.test.js`'s unit-level
 * reproduction): the anchor row carries an EMPTY `accounts` Set (it isn't
 * a real position — nothing to add), so `pairGroupSort`'s
 * `${account}|${tier}|${pair_group_key}` key starts directly with the `|`
 * separator (char code 0x7C) once the account segment is empty — which
 * sorts AFTER every real account code (uppercase letters/digits, all
 * < 0x7C). `pairGroupPostSort` (pulseGridSetup.js) now partitions each
 * underlying block into anchor row(s) (always first) and everything else
 * (sorted via the existing `pairGroupSort`), instead of running the whole
 * block through one key-based sort where the anchor's missing account
 * always loses.
 *
 * Also covers the label fix: the anchor row shows the virtual-root label
 * ("GOLD") via the shared `formatPositionSymbol()` (rootOf.js) — the SAME
 * function the derivatives Legs grid's own underlying-anchor row now
 * calls (see derivatives_legs_underlying_anchor.spec.js), not the raw
 * dated contract.
 *
 * Fixture pattern: fetch the REAL /api/positions/ envelope and override
 * only `rows` — same precedent as position_lot_pair_split.spec.js.
 *
 * Run:
 *   npx playwright test e2e/pulse_underlying_anchor_sort.spec.js --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;
const ACCT = 'ZG0790';

// Same-expiry-month as the real current GOLD front-month future (whatever
// month that is "today" — MCX GOLD options' own last-trading-day is a few
// business days before their matching future's expiry, so hardcoding a
// specific month here would drift and fall into the "no same-month future"
// far-month fallback edge case once that month's own future rolls off the
// instruments master). Resolved dynamically in `test.beforeAll` below.
let GOLD_ROWS = [];

const POSITIONS_RE = /\/api\/positions\/(\?.*)?$/;

async function mockPositions(/** @type {import('@playwright/test').Page} */ page) {
  await page.route(POSITIONS_RE, async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    let body = { rows: [] };
    try {
      const real = await route.fetch();
      body = { ...(await real.json()) };
    } catch (_) {
      // backend unreachable — minimal envelope below
    }
    body.rows = GOLD_ROWS;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

test.beforeAll(async ({ request }) => {
  // Discover two REAL, currently-listed GOLD options expiring in the SAME
  // month as the current front-month GOLD future, so resolveUnderlyingForOption's
  // same-month match succeeds (avoids the "no same-month future" far-month
  // fallback, which would make the anchor's label legitimately the raw
  // dated contract instead of the bare virtual root "GOLD" — a different,
  // already-covered code path, not what this spec is testing).
  const res = await request.get('/api/instruments/');
  const data = await res.json();
  const items = data.items || [];
  const futs = items
    .filter((i) => i.e === 'MCX' && i.t === 'FUT' && String(i.u || '').toUpperCase() === 'GOLD')
    .sort((a, b) => (a.x < b.x ? -1 : 1));
  const frontMonth = futs[0]?.x?.slice(0, 7); // YYYY-MM
  const opts = items.filter((i) =>
    i.e === 'MCX' && (i.t === 'CE' || i.t === 'PE') &&
    String(i.u || '').toUpperCase() === 'GOLD' && i.x?.slice(0, 7) === frontMonth);
  const ce = opts.find((o) => o.t === 'CE');
  const pe = opts.find((o) => o.t === 'PE' && o.k !== ce?.k);
  if (!ce || !pe) throw new Error('No same-month GOLD CE/PE pair found in live instruments — cannot build fixture');

  GOLD_ROWS = [
    {
      tradingsymbol: ce.s, symbol: ce.s, exchange: 'MCX',
      account: ACCT, product: 'NRML', quantity: 100, average_price: ce.k * 0.02, last_price: ce.k * 0.015,
      pnl: -30600, realised: 0, unrealised: -30600, day_pnl: -30600, prev_settlement_pnl: 0,
      close_price: ce.k * 0.02, lot_size: ce.ls,
    },
    {
      tradingsymbol: pe.s, symbol: pe.s, exchange: 'MCX',
      account: ACCT, product: 'NRML', quantity: -100, average_price: pe.k * 0.005, last_price: pe.k * 0.004,
      pnl: 8650, realised: 0, unrealised: 8650, day_pnl: 8650, prev_settlement_pnl: 0,
      close_price: pe.k * 0.005, lot_size: pe.ls,
    },
  ];
});

test.beforeEach(async ({ page }) => {
  await loginAsAdmin(page);
  await mockPositions(page);
});

test.describe('MarketPulse Positions grid — underlying-anchor row sort + label', () => {
  test('the GOLD underlying-anchor row renders FIRST within its block, ahead of its own CE legs', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const posGrid = page.locator('.pulse-bucket-positions, [data-bucket="positions"]').first();
    // Fall back to any grid containing a GOLD row if the bucket container
    // selector drifts — the assertion below is on row ORDER, not container.
    const goldRows = page.locator('.ag-row:has-text("GOLD")');
    await expect(goldRows.first()).toBeVisible({ timeout: TIMEOUT });
    // Give the instruments cache (and the symbol-label memo it busts on
    // load — MarketPulse.svelte's `_pulseSymFmtCache.clear()` on
    // `instrumentsCacheVersion` change) a moment to land and re-render —
    // a genuinely cold page load can paint the anchor's raw-contract
    // label for one frame before instruments finish loading.
    await page.waitForTimeout(3000);

    const count = await goldRows.count();
    expect(count).toBeGreaterThanOrEqual(3); // 1 anchor + 2 real CE legs

    // The anchor row is the one whose Qty cell is 0/blank — real legs
    // carry nonzero quantity. Assert it's the FIRST GOLD row rendered.
    const firstRowText = await goldRows.first().textContent();
    const laterRowsText = await Promise.all(
      Array.from({ length: count - 1 }, (_, i) => goldRows.nth(i + 1).textContent())
    );
    // The anchor's own symbol label is bare "GOLD" (virtual root) — a real
    // CE/PE leg's symbol always carries a strike digit right after "GOLD"
    // (e.g. "GOLD26DEC119000CE"), so "GOLD" NOT immediately followed by a
    // digit distinguishes the anchor from every real leg without needing
    // a specific CSS hook.
    expect(firstRowText).toMatch(/GOLD(?!\d)/);
    for (const t of laterRowsText) {
      expect(t).not.toBe(firstRowText);
    }
  });

  test('the anchor row label is the virtual root "GOLD", not a dated contract', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const goldRows = page.locator('.ag-row:has-text("GOLD")');
    await expect(goldRows.first()).toBeVisible({ timeout: TIMEOUT });
    await page.waitForTimeout(3000); // see sibling test's comment on instrumentsCacheVersion
    const firstRowText = (await goldRows.first().textContent()) || '';
    // Must not contain a dated-contract suffix — those belong to the
    // resolved tradingsymbol, not the virtual-root display label.
    expect(firstRowText).not.toMatch(/FUT|\d{2}[A-Z]{3}/);
  });
});
