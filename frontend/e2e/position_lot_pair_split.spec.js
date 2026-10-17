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
