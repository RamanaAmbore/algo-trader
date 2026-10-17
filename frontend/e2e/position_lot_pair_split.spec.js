/**
 * position_lot_pair_split.spec.js
 *
 * Regression guard for the client-side lot-pairing split
 * (`frontend/src/lib/data/lotPairSplit.js:splitPairedLots`), wired into:
 *   - MarketPulse Positions grid  (`.badge-lotsplit` chip via _symCellBadges)
 *   - Derivatives Legs grid       (`.cand-lotsplit-tag` chip in CandidateLegRow)
 *
 * Fixture (one account, one NIFTY root, mocked via page.route on
 * GET /api/positions/ — the same interception precedent as
 * derivatives_auto_default.spec.js / avg_direction_tint.spec.js):
 *   NIFTY26JUN24000CE  qty +150   pnl  150   -> splits into P1 (100) + P2 (50)
 *   NIFTY26JUN24500CE  qty -100   pnl  100   -> single pair, P1 counterparty
 *   NIFTY26JUN24600CE  qty  -50   pnl    0   -> single pair, P2 counterparty
 * Expected after split: 3 display rows for 2 broker positions; the split
 * pieces of 24000CE carry pnl 100 (P1) + 50 (P2); Σ pnl stays 250 (a
 * double-count of the split would read 400).
 *
 * Covers:
 *   (a) Pulse Positions grid shows one `.badge-lotsplit` chip per split
 *       piece, with labels P1 and P2.
 *   (b) The Positions TOTAL row P&L equals the un-split sum (2500 -> "2,500").
 *   (c) Labels are identical across three consecutive positions loads
 *       (stable keys — no flicker between polls).
 *   (d) Derivatives Legs grid shows the same P1/P2 `.cand-lotsplit-tag` chips.
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

/** Sorted labels of every lot-split chip currently in the Pulse Positions grid. */
async function pulseLabels(/** @type {import('@playwright/test').Page} */ page) {
  await expect(page.locator('.badge-lotsplit')).toHaveCount(2, { timeout: TIMEOUT });
  const texts = await page.locator('.badge-lotsplit').allTextContents();
  return texts.map((t) => t.trim()).sort();
}

test.beforeEach(async ({ page }) => {
  await loginAsAdmin(page);
  await mockPositions(page);
});

test.describe('Lot-pairing split — MarketPulse Positions grid', () => {
  test('(a) one broker position split into P1/P2 display rows, each with a .badge-lotsplit chip', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    const labels = await pulseLabels(page);
    expect(labels).toEqual(['P1', 'P2']);
  });

  test('(b) Positions TOTAL day P&L is unchanged by the split (sum of raw pnl = 250)', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await pulseLabels(page);
    // The TOTAL pinned row is split across a left (symbol) grid and a right
    // (numeric) grid — the numeric cells live in the right grid's row.
    const totalDayPnl = page.locator('[row-id="__total_positions"] [col-id="day_pnl"]');
    await expect(totalDayPnl).toHaveText(/^\+?250$/, { timeout: TIMEOUT });
  });

  test('(c) lot_piece_label values are stable across three consecutive positions loads', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    const first = await pulseLabels(page);

    for (let i = 0; i < 2; i++) {
      const resp = page.waitForResponse((r) => POSITIONS_RE.test(r.url()) && r.request().method() === 'GET');
      await page.reload({ waitUntil: 'domcontentloaded' });
      await resp;
      expect(await pulseLabels(page)).toEqual(first);
    }
    expect(first).toEqual(['P1', 'P2']);
  });
});

test.describe('Lot-pairing split — Derivatives Legs grid', () => {
  test('(d) Legs grid shows the same P1/P2 .cand-lotsplit-tag chips for the split leg', async ({ page }) => {
    await page.goto('/admin/derivatives?u=NIFTY', { waitUntil: 'domcontentloaded' });
    const tags = page.locator('.cand-lotsplit-tag');
    await expect(tags).toHaveCount(2, { timeout: TIMEOUT });
    const texts = (await tags.allTextContents()).map((t) => t.trim()).sort();
    expect(texts).toEqual(['P1', 'P2']);
  });
});
