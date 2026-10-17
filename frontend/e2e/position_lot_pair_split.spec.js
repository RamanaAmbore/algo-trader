/**
 * position_lot_pair_split.spec.js
 *
 * Regression guard for the client-side lot-pairing split
 * (`frontend/src/lib/data/lotPairSplit.js:splitPairedLots`), wired into:
 *   - MarketPulse Positions grid  (one display row per split piece)
 *   - Derivatives Legs grid       (one row per split piece in CandidateLegRow)
 *
 * The redundant cyan `.badge-lotsplit` / `.cand-lotsplit-tag` chips that
 * originally shipped alongside this feature were removed as duplicative of
 * the existing St-column badge (`positionStateBadge.js`) — for the
 * "Unmatched" leftover piece specifically, the cyan chip visually read as
 * "paired" even though the St column correctly showed the amber orphan
 * state right next to it. This spec now asserts those chips are ABSENT and
 * that the St-column badge shows the correct state for each split piece.
 *
 * Fixture (one account, one NIFTY root, mocked via page.route on
 * GET /api/positions/ — the same interception precedent as
 * derivatives_auto_default.spec.js / avg_direction_tint.spec.js):
 *   NIFTY26JUN24000CE  qty +150   pnl  150   -> splits into P1 (100) + P2 (50)
 *   NIFTY26JUN24500CE  qty -100   pnl  100   -> single pair, P1 counterparty
 *   NIFTY26JUN24600CE  qty  -50   pnl    0   -> single pair, P2 counterparty
 * Expected after split: 4 display rows for 3 broker positions; the split
 * pieces of 24000CE carry pnl 100 (P1) + 50 (P2); Σ pnl stays 250 (a
 * double-count of the split would read 400).
 *
 * Covers:
 *   (a) Pulse Positions grid has no `.badge-lotsplit` chip; the St column
 *       shows the correct `paired` state (P1/P2) for each split piece.
 *   (b) The Positions TOTAL row day P&L equals the un-split sum (250).
 *   (c) St-column pair labels for the split pieces are stable across three
 *       consecutive positions loads (stable keys — no flicker between polls).
 *   (d) Derivatives Legs grid has no `.cand-lotsplit-tag` chip; the St-column
 *       badges show the correct `paired` state (P1 x2, P2 x2 — the split
 *       pieces plus their single-row counterparty legs) for the split leg.
 *   (e) Toggling one split piece's checkbox doesn't toggle its sibling piece
 *       — regression guard for an `enKey(c)` collision (2026-10) where the
 *       key was `${account}|${symbol}` only, so both lot-split display rows
 *       of one position shared a single enabledSymbols entry.
 *   (f) St column header is horizontally centered in both grids, matching
 *       the already-centered body badge (2026-10 cosmetic fix).
 *
 * Run:
 *   npx playwright test e2e/position_lot_pair_split.spec.js --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;
const ACCT = 'ZG0790';

const LOT_ROWS = [
  {
    tradingsymbol: 'NIFTY26JUN24000CE', symbol: 'NIFTY26JUN24000CE', exchange: 'NFO',
    account: ACCT, product: 'NRML', quantity: 150, average_price: 100, last_price: 110,
    pnl: 150, realised: 0, unrealised: 150, day_pnl: 0, prev_settlement_pnl: 0,
    close_price: 100,
  },
  {
    tradingsymbol: 'NIFTY26JUN24500CE', symbol: 'NIFTY26JUN24500CE', exchange: 'NFO',
    account: ACCT, product: 'NRML', quantity: -100, average_price: 50, last_price: 40,
    pnl: 100, realised: 0, unrealised: 100, day_pnl: 0, prev_settlement_pnl: 0,
    close_price: 50,
  },
  {
    tradingsymbol: 'NIFTY26JUN24600CE', symbol: 'NIFTY26JUN24600CE', exchange: 'NFO',
    account: ACCT, product: 'NRML', quantity: -50, average_price: 30, last_price: 30,
    pnl: 0, realised: 0, unrealised: 0, day_pnl: 0, prev_settlement_pnl: 0,
    close_price: 30,
  },
];

// Intraday round-trip on an unrelated root (BANKNIFTY, isolated from the
// NIFTY group above so it can't shift the NIFTY waterfall's P1/P2
// numbering, and kept OUT of the shared LOT_ROWS fixture so it can't shift
// the MarketPulse TOTAL row's day P&L in test (b)/(c)) — bought 100, sold
// 80 today, net long 20 still open. `splitClosedReopened` (expiryPnl.js)
// tags the two pieces `_splitTag` 'closed'/'open' and neither gets a
// `lot_piece_label` (that's lotPairSplit.js's own, DIFFERENT split
// mechanism) — before the enKey fix this pair collided on
// `${account}|${symbol}|` (test (g) below, which mocks its own response).
const SPLIT_TAG_ROW = {
  tradingsymbol: 'BANKNIFTY26JUN50000CE', symbol: 'BANKNIFTY26JUN50000CE', exchange: 'NFO',
  account: ACCT, product: 'NRML', quantity: 20, average_price: 100, last_price: 101,
  pnl: 80, realised: 80, unrealised: 0, day_pnl: 80, prev_settlement_pnl: 0,
  close_price: 100, overnight_quantity: 0,
  day_buy_quantity: 100, day_sell_quantity: 80, day_buy_value: 10000, day_sell_value: 8080,
};

const POSITIONS_RE = /\/api\/positions\/(\?.*)?$/;

/**
 * Serve the real /api/positions envelope with only `rows` replaced by the
 * lot-split fixture. Falls back to a minimal envelope when the backend is
 * unreachable (no live broker data is needed for this assertion).
 */
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
    body.rows = LOT_ROWS;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

// The 24000CE row splits into two display rows keyed
// `${symbol}::${account}::${label}` (mergePositionRows composite key,
// pulseUnified.js) with the Positions-bucket suffix (`BUCKET_SUFFIX.positions`,
// pulseUnified.js) appended — this IS the ag-Grid `row-id` (pulseRowId reads
// `data.key`). Scoping assertions to these two row-ids (rather than every
// `paired`-state badge in the grid, which also includes the two
// counterparty rows' own single-row P1/P2 badges) keeps the check as
// precise as the old `.badge-lotsplit`-count assertion was.
const SPLIT_SYM = 'NIFTY26JUN24000CE';
const P1_ROW_ID = `${SPLIT_SYM}::${ACCT}::P1__pos`;
const P2_ROW_ID = `${SPLIT_SYM}::${ACCT}::P2__pos`;

/** Sorted St-column glyph text for the two lot-split display rows of the split leg. */
async function pulseSplitStateLabels(/** @type {import('@playwright/test').Page} */ page) {
  const p1 = page.locator(`[row-id="${P1_ROW_ID}"] .pos-state-badge-btn`);
  const p2 = page.locator(`[row-id="${P2_ROW_ID}"] .pos-state-badge-btn`);
  await expect(p1).toHaveCount(1, { timeout: TIMEOUT });
  await expect(p2).toHaveCount(1, { timeout: TIMEOUT });
  const texts = await Promise.all([p1.textContent(), p2.textContent()]);
  return texts.map((t) => (t || '').trim()).sort();
}

test.beforeEach(async ({ page }) => {
  await loginAsAdmin(page);
  await mockPositions(page);
});

test.describe('Lot-pairing split — MarketPulse Positions grid', () => {
  test('(a) one broker position split into P1/P2 display rows; no redundant chip, St column shows correct paired state', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    const labels = await pulseSplitStateLabels(page);
    expect(labels).toEqual(['P1', 'P2']);
    // Redundant chip removed (2026-10) — St column above is now the sole
    // signal for split-piece pairing state.
    await expect(page.locator('.badge-lotsplit')).toHaveCount(0);
  });

  test('(b) Positions TOTAL day P&L is unchanged by the split (sum of raw pnl = 250)', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await pulseSplitStateLabels(page);
    // The TOTAL pinned row is split across a left (symbol) grid and a right
    // (numeric) grid — the numeric cells live in the right grid's row.
    const totalDayPnl = page.locator('[row-id="__total_positions"] [col-id="day_pnl"]');
    await expect(totalDayPnl).toHaveText(/^\+?250$/, { timeout: TIMEOUT });
  });

  test('(c) St-column pair labels for the split pieces are stable across three consecutive positions loads', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    const first = await pulseSplitStateLabels(page);

    for (let i = 0; i < 2; i++) {
      const resp = page.waitForResponse((r) => POSITIONS_RE.test(r.url()) && r.request().method() === 'GET');
      await page.reload({ waitUntil: 'domcontentloaded' });
      await resp;
      expect(await pulseSplitStateLabels(page)).toEqual(first);
    }
    expect(first).toEqual(['P1', 'P2']);
  });
});

test.describe('Lot-pairing split — Derivatives Legs grid', () => {
  test('(d) Legs grid has no redundant chip; St-column badges show correct paired state for the split leg', async ({ page }) => {
    await page.goto('/admin/derivatives?u=NIFTY', { waitUntil: 'domcontentloaded' });
    // Redundant chip removed (2026-10).
    await expect(page.locator('.cand-lotsplit-tag')).toHaveCount(0);
    // 4 `paired` St badges: the split leg's two pieces (P1, P2) plus the
    // two single-row counterparty legs (24500CE -> P1, 24600CE -> P2).
    const paired = page.locator('.cand-state-badge-btn[data-pos-state="paired"]');
    await expect(paired).toHaveCount(4, { timeout: TIMEOUT });
    const texts = (await paired.allTextContents()).map((t) => t.trim()).sort();
    expect(texts).toEqual(['P1', 'P1', 'P2', 'P2']);
  });

  test('(e) toggling one split piece\'s checkbox does not toggle its sibling piece (enKey collision regression)', async ({ page }) => {
    // Regression guard for the `enKey(c)` composite key in +page.svelte —
    // before the fix it was `${account}|${symbol}` only, so the two
    // lot-split display rows of 24000CE (P1, P2) shared ONE enabledSymbols
    // entry: unchecking P1's checkbox silently unchecked P2's too (reported
    // as "when you select one row in legs, it selects... an additional
    // row"). `data-lot-piece` on `.cand-row` (CandidateLegRow.svelte)
    // disambiguates the two rows for this test — only split pieces carry
    // it, so it unambiguously targets P1 vs P2 of the SAME symbol.
    await page.goto('/admin/derivatives?u=NIFTY', { waitUntil: 'domcontentloaded' });
    const p1Row = page.locator('.cand-row[data-lot-piece="P1"]');
    const p2Row = page.locator('.cand-row[data-lot-piece="P2"]');
    await expect(p1Row).toHaveCount(1, { timeout: TIMEOUT });
    await expect(p2Row).toHaveCount(1, { timeout: TIMEOUT });
    const p1Check = p1Row.locator('input[type="checkbox"]');
    const p2Check = p2Row.locator('input[type="checkbox"]');
    await expect(p1Check).toBeChecked();
    await expect(p2Check).toBeChecked();

    await p1Check.uncheck();
    await expect(p1Check).not.toBeChecked();
    await expect(p2Check).toBeChecked();

    await p2Check.uncheck();
    await expect(p1Check).not.toBeChecked();
    await expect(p2Check).not.toBeChecked();
  });

  test('(f) St column header is horizontally centered, matching the centered body badge', async ({ page }) => {
    await page.goto('/admin/derivatives?u=NIFTY', { waitUntil: 'domcontentloaded' });
    const hdr = page.locator('.cand-headrow .cand-state-hdr');
    await expect(hdr).toHaveCount(1, { timeout: TIMEOUT });
    const textAlign = await hdr.evaluate((el) => getComputedStyle(el).textAlign);
    expect(textAlign).toBe('center');
  });

  test('(g) toggling the CLOSED piece\'s checkbox does not toggle the OPEN sibling (enKey _splitTag collision regression)', async ({ page }) => {
    // Sibling regression to (e) — a DIFFERENT split mechanism
    // (splitClosedReopened's closed/open pieces, expiryPnl.js) shares the
    // same `${account}|${symbol}` base as lotPairSplit.js's P1/P2/Unmatched
    // pieces but tags via `_splitTag`, not `lot_piece_label`. Before the
    // 2026-10-17 fix, `enKey` only disambiguated on `lot_piece_label`, so
    // these two pieces (neither carrying one) still collided on one
    // `enabledSymbols` entry.
    // Own route override, scoped to this test only — [...LOT_ROWS, SPLIT_TAG_ROW]
    // keeps the shared fixture's other tests (whose totals/counts are
    // pinned to exactly the original 3 rows) unaffected.
    await page.route(POSITIONS_RE, async (route) => {
      if (route.request().method() !== 'GET') { await route.continue(); return; }
      let body = { rows: [] };
      try {
        const real = await route.fetch();
        body = { ...(await real.json()) };
      } catch (_) { /* backend unreachable — minimal envelope below */ }
      body.rows = [...LOT_ROWS, SPLIT_TAG_ROW];
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });
    await page.goto('/admin/derivatives?u=BANKNIFTY', { waitUntil: 'domcontentloaded' });
    const closedRow = page.locator('.cand-row:has(.cand-split-closed)');
    const openRow = page.locator('.cand-row:has(.cand-split-open)');
    await expect(closedRow).toHaveCount(1, { timeout: TIMEOUT });
    await expect(openRow).toHaveCount(1, { timeout: TIMEOUT });
    const closedCheck = closedRow.locator('input[type="checkbox"]');
    const openCheck = openRow.locator('input[type="checkbox"]');
    await expect(closedCheck).toBeChecked();
    await expect(openCheck).toBeChecked();

    await closedCheck.uncheck();
    await expect(closedCheck).not.toBeChecked();
    await expect(openCheck).toBeChecked();

    await openCheck.uncheck();
    await expect(closedCheck).not.toBeChecked();
    await expect(openCheck).not.toBeChecked();
  });
});

test.describe('St column header centering — MarketPulse Positions grid', () => {
  test('header label is centered, matching the centered .ag-cell-pair-state body badge', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await pulseSplitStateLabels(page);
    const hdrLabel = page.locator('[col-id="pos_state"] .ag-header-cell-label');
    await expect(hdrLabel).toHaveCount(1, { timeout: TIMEOUT });
    const justify = await hdrLabel.evaluate((el) => getComputedStyle(el).justifyContent);
    expect(justify).toBe('center');
  });
});

// ============================================================================
// Part A (2026-10) — one row PER ACCOUNT, not one merged cross-account row.
//
// A symbol held in two different accounts used to merge into a single
// aggregated MarketPulse row with is_orphan/pair_group_key/pin_group_key/
// has_gtt reset to null (ambiguous — which account's state applies?).
// mergePositionRows (pulseUnified.js) now keys every position row by
// `${symbol}::${account}`, so this symbol instead renders as TWO separate
// rows, each carrying its OWN correct St-column state — and a thin
// symbol-subtotal row (buildPositionsDisplayRows) above them preserves the
// "combined exposure across accounts" read the old merged row used to give
// for free.
// ============================================================================

const ACCT2 = 'ZJ6294';
const TWO_ACCT_SYM = 'NIFTY26JUN25500CE';

const TWO_ACCT_ROWS = [
  {
    tradingsymbol: TWO_ACCT_SYM, symbol: TWO_ACCT_SYM, exchange: 'NFO',
    account: ACCT, product: 'NRML', quantity: 40, average_price: 80, last_price: 85,
    pnl: 200, realised: 0, unrealised: 200, day_pnl: 50, prev_settlement_pnl: 0,
    close_price: 80,
  },
  {
    tradingsymbol: TWO_ACCT_SYM, symbol: TWO_ACCT_SYM, exchange: 'NFO',
    account: ACCT2, product: 'NRML', quantity: -20, average_price: 40, last_price: 38,
    pnl: 40, realised: 0, unrealised: 40, day_pnl: 10, prev_settlement_pnl: 0,
    close_price: 40,
  },
];

const TWO_ACCT_ROW_ID_1 = `${TWO_ACCT_SYM}::${ACCT}__pos`;
const TWO_ACCT_ROW_ID_2 = `${TWO_ACCT_SYM}::${ACCT2}__pos`;
const TWO_ACCT_SUBTOTAL_ROW_ID = `__subtotal_pos_${TWO_ACCT_SYM}`;

test.describe('Per-account Positions rows + symbol-subtotal (2026-10 Part A)', () => {
  test.beforeEach(async ({ page }) => {
    // Own route override — these rows are isolated to this describe block
    // only, so they can't shift the LOT_ROWS-based tests' totals/counts
    // above (same precedent as test (g)'s own override).
    await page.route(POSITIONS_RE, async (route) => {
      if (route.request().method() !== 'GET') { await route.continue(); return; }
      let body = { rows: [] };
      try {
        const real = await route.fetch();
        body = { ...(await real.json()) };
      } catch (_) { /* backend unreachable — minimal envelope below */ }
      body.rows = TWO_ACCT_ROWS;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });
  });

  test('a symbol held in 2 accounts renders 2 separate rows, each with its own St-column badge', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    // ag-Grid renders pinned-left columns (the 'St' column included) in a
    // SEPARATE DOM row from the center-viewport columns, both carrying the
    // SAME `row-id` attribute — so a bare `[row-id="..."]` locator always
    // resolves to 2 elements per logical row in this grid, by ag-Grid's own
    // design (not a bug). Scoping directly to `.pos-state-badge-btn` (which
    // only exists inside the pinned-left copy, since 'St' is pinned) keeps
    // this assertion precise regardless of that duplication.
    const badge1 = page.locator(`[row-id="${TWO_ACCT_ROW_ID_1}"] .pos-state-badge-btn`);
    const badge2 = page.locator(`[row-id="${TWO_ACCT_ROW_ID_2}"] .pos-state-badge-btn`);
    await expect(badge1).toHaveCount(1, { timeout: TIMEOUT });
    await expect(badge2).toHaveCount(1, { timeout: TIMEOUT });

    // Each account is alone in its own (account, root, opt_type) pairing
    // group (no counterparty leg in the SAME account) — both orphan, and
    // crucially NOT blank/nulled the way the old ambiguity-guard used to
    // render a 2-account merged row.
    await expect(badge1).toHaveAttribute('data-pos-state', 'orphan');
    await expect(badge2).toHaveAttribute('data-pos-state', 'orphan');
  });

  test('the symbol-subtotal row shows the correct combined qty + day P&L for both accounts', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    // See the ag-Grid pinned-left/center row-duplication note above — these
    // column-scoped locators each resolve to exactly 1 real element
    // (qty_net/day_pnl live in the center viewport only; the 'St' badge
    // lives in the pinned-left viewport only) without asserting a bare
    // row-id count.
    const qtyCell = page.locator(`[row-id="${TWO_ACCT_SUBTOTAL_ROW_ID}"] [col-id="qty_net"]`);
    const dayPnlCell = page.locator(`[row-id="${TWO_ACCT_SUBTOTAL_ROW_ID}"] [col-id="day_pnl"]`);
    const subtotalBadge = page.locator(`[row-id="${TWO_ACCT_SUBTOTAL_ROW_ID}"] .pos-state-badge-btn`);

    // Combined qty: 40 + (-20) = 20.
    await expect(qtyCell).toHaveText(/^20$/, { timeout: TIMEOUT });

    // Combined day P&L: mergePositionRows recomputes day_pnl via the
    // baseline-diff SSOT formula (realised + unrealised − prev_settlement_pnl),
    // not the raw `day_pnl` field in the mock — row A: 0+200−0=200,
    // row B: 0+40−0=40, combined 240.
    await expect(dayPnlCell).toHaveText(/^\+?240$/, { timeout: TIMEOUT });

    // The subtotal row carries no single-account St-column state.
    await expect(subtotalBadge).toHaveCount(0);
  });
});
