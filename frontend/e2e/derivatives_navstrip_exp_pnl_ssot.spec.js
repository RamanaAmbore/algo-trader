/**
 * derivatives_navstrip_exp_pnl_ssot.spec.js
 *
 * NavStrip P slot 3 (Exp P&L) vs derivatives Snapshot grid TOTAL — SSOT fix
 * (2026-09). Root cause: NavStrip (portfolioStore.svelte.js) derived the
 * *realised* component of Exp P&L from the broker's raw, documented-
 * unreliable `realised` field, while the Snapshot grid ran every position
 * through `splitClosedReopened` (derivatives/pageLoad.js) first — a precise
 * closed/open split rebuilt from entry/exit-price math. On an ordinary
 * same-day partial/full close (Kite ships `realised: 0` alongside a real
 * settlement `pnl` — docs/specs/PULSE_SPEC.md:1343) the two surfaces could
 * diverge by exactly the closed portion's realised P&L.
 *
 * Fix: the split-aware derivation (`splitClosedReopened` + the both-zero-
 * fields-fall-back-to-pnl convention) now lives once in
 * `frontend/src/lib/data/expiryPnl.js` (`positionExpPnl`). portfolioStore
 * calls it to build its own canonical `exp_pnl` per row (so NavStrip's
 * total is correct at the source) AND exposes a per-row array
 * (`portfolioStore.positions.expPnlRows`) that the Snapshot grid now reads
 * (filtered by account/strategy) instead of recomputing exp_pnl
 * independently.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *   SSOT   — both surfaces read the SAME `positionExpPnl`-derived values;
 *            grep confirms neither computes exp_pnl from a second copy of
 *            the split logic.
 *   Perf   — code-level guards are pure fs reads; the browser scenario is
 *            a single page load with two mocked API routes, no extra
 *            round-trips.
 *   Stale  — guards that `pageLoad.js` re-exports (not redefines)
 *            splitClosedReopened/buildPositionRowFromBroker, and that the
 *            old per-row Exp P&L recomputation paths were replaced.
 *   Reuse  — `positionExpPnl` is the single call site portfolioStore uses;
 *            `_reduceStoreExpRows` is the single reduction Snapshot uses
 *            for both Exp P&L and Extrinsic.
 *   UX     — NavStrip's Exp P&L pill and the Snapshot TOTAL row are both
 *            visible together and show the SAME number for an unfiltered,
 *            live (non-sim) worked example.
 *
 * The browser fixture is deliberately FUTURES-only (no options) so it does
 * not depend on instruments-cache / underlying-quote mocks: a future's
 * expiry anchor resolves to its own `last_price` (resolveExpiryAnchor's
 * `ownPolledLtp` tier) when no live SSE tick has landed yet, which is
 * always true on a freshly-loaded mocked page. Expected value (75,000 —
 * "75K" once both surfaces run it through `aggCompact`, the SAME compact-
 * money formatter `fmtMoney` (NavStrip) and the Snapshot TOTAL row both
 * use) is cross-checked against `positionExpPnl` directly via `vite-node`
 * before being hardcoded here (see `/tmp` scratch verification in the
 * implementing session — reproduced inline below for reference):
 *   closed leg:  (24000 − 23900) × 250 = 25,000
 *   open leg:    (23800 − 24000) × −250 = 50,000
 *   split total: 75,000 → "75K"
 *   naive (pre-fix) total: 50,000 (open leg only) → "50K" — the number
 *   this spec would show if the SSOT fix regressed.
 *
 * Run:
 *   cd frontend && npx playwright test derivatives_navstrip_exp_pnl_ssot --workers=1
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE    = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const TIMEOUT = 25_000;

// ── Code-level SSOT guards (no browser required) ─────────────────────────────

test.describe('Code-level SSOT guards', () => {
  test('expiryPnl.js owns positionExpPnl + splitClosedReopened + buildPositionRowFromBroker', () => {
    const src = readFileSync('src/lib/data/expiryPnl.js', 'utf8');
    expect(src).toContain('export function positionExpPnl(');
    expect(src).toContain('export function splitClosedReopened(');
    expect(src).toContain('export function buildPositionRowFromBroker(');
    // The both-zero-fields-fall-back-to-pnl fix — `realised` trusted only
    // when truthy (non-zero), matching nav.js's currentTotalProfit exactly.
    expect(src).toContain('if (realised) return realised;');
  });

  test('pageLoad.js re-exports (does not redefine) splitClosedReopened/buildPositionRowFromBroker', () => {
    const src = readFileSync('src/lib/derivatives/pageLoad.js', 'utf8');
    expect(src).toContain("export { buildPositionRowFromBroker, splitClosedReopened } from '$lib/data/expiryPnl.js';");
    // The old inline definitions must be gone — only ONE function named
    // splitClosedReopened/buildPositionRowFromBroker may exist in this repo
    // (in expiryPnl.js), not a second copy here.
    expect(src).not.toContain('export function splitClosedReopened(');
    expect(src).not.toContain('export function buildPositionRowFromBroker(');
  });

  test('portfolioStore.svelte.js derives exp_pnl via positionExpPnl, not the raw broker `realised` field directly', () => {
    const src = readFileSync('src/lib/data/portfolioStore.svelte.js', 'utf8');
    expect(src).toContain('positionExpPnl(p, kind, anchor)');
    expect(src).toContain('positionExpPnl(p, kind, null)');
    // Old buggy call sites (cRow built with raw p.realised passed straight
    // into expiryPnlWithRealised) must be gone.
    expect(src).not.toContain('realised: p?.realised, pnl: p?._pnl');
    // Store exposes the per-row array Snapshot now reads.
    expect(src).toContain('expPnlRows');
  });

  test('derivatives Snapshot grid reduces portfolioStore.positions.expPnlRows in live mode instead of recomputing exp_pnl independently', () => {
    const src = readFileSync('src/routes/(algo)/admin/derivatives/+page.svelte', 'utf8');
    expect(src).toContain('function _reduceStoreExpRows(field, matchStrategy)');
    expect(src).toContain('portfolioStore.positions.expPnlRows');
    // Both filtered reductions branch on simActive and fall through to the
    // store in live mode.
    const filteredExp = src.slice(src.indexOf('const _filteredExpPnlByRoot'), src.indexOf('const _filteredExpPnlByRoot') + 400);
    expect(filteredExp).toContain('simActive');
    expect(filteredExp).toContain("_reduceStoreExpRows('exp_pnl', matchStrategy)");
  });

  test('Snapshot TOTAL Exp P&L tooltip no longer unconditionally claims NavStrip P-slot-3 equality', () => {
    const src = readFileSync('src/routes/(algo)/admin/derivatives/+page.svelte', 'utf8');
    expect(src).not.toContain('F&O-only expiry P&L for this group. Sums to the NavStrip P slot 3 value.');
    expect(src).toContain('Sum of the rows shown below');
  });
});

// ── Browser: NavStrip Exp P&L == Snapshot TOTAL Exp P&L for a mocked,
//    Kite-style partial-close position (no account/strategy/search filter) ──

/**
 * Futures partial-close fixture — overnight short 500 NIFTY25SEPFUT @24000,
 * 250 bought back today @23900 (realising 25,000 on the closed portion),
 * spot (own last_price, no live tick) 23800 → open portion unrealised
 * (23800-24000)*(-250) = 50,000. Split-aware total = 25,000 + 50,000 =
 * 75,000 ("75K" after aggCompact). The pre-fix (naive, unsplit)
 * computation would have shown 50,000 ("50K") — the open portion only,
 * silently dropping the 25,000 closed leg because Kite ships
 * `realised: 0` on this row.
 */
function futuresFixtureRow() {
  return {
    tradingsymbol: 'NIFTY25SEPFUT',
    exchange: 'NFO',
    account: 'ACC1',
    quantity: -250,
    average_price: 24000,
    last_price: 23800,
    prev_close: 24000,
    overnight_quantity: -500,
    day_buy_quantity: 250,
    day_sell_quantity: 0,
    day_buy_value: 5975000, // 250 @ 23900
    day_sell_value: 0,
    pnl: 10000,
    realised: 0,
    day_change_val: 0,
    product: 'NRML',
  };
}

async function mockDerivativesData(page) {
  await page.route('**/api/market/status**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ nse_open: true, mcx_open: true, any_open: true, is_holiday: false }),
    });
  });
  await page.route('**/api/positions**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ source: 'live', rows: [futuresFixtureRow()], stale_accounts: [], as_of: null }),
    });
  });
  await page.route('**/api/holdings**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ source: 'live', rows: [], stale_accounts: [], as_of: null }),
    });
  });
}

test.describe('Browser: NavStrip Exp P&L pill == Snapshot TOTAL Exp P&L (unfiltered)', () => {
  test.setTimeout(60_000);
  test.beforeEach(async ({ page }) => {
    await mockDerivativesData(page);
    await loginAsAdmin(page);
  });

  test('mocked partial-close futures position: NavStrip P slot 3 equals Snapshot TOTAL Exp P&L (both "75K")', async ({ page }) => {
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded' });

    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });
    const pExpCell = strip.locator('.ps-agg').first().locator('.ps-exp');
    await expect(pExpCell).toBeVisible({ timeout: TIMEOUT });

    const snapCard = page.locator('.opt-byund-card');
    await expect(snapCard).toBeVisible({ timeout: TIMEOUT });
    const totalRow = snapCard.locator('.byund-row-total');
    await expect(totalRow).toBeVisible({ timeout: TIMEOUT });

    // Both NavStrip's pill (fmtMoney) and the Snapshot TOTAL row
    // (aggCompact) render through the SAME compact-money formatter, so
    // 75,000 → "75K" on both surfaces. The pre-fix (naive, unsplit) value
    // would render as "50K" instead — a visually distinct, unambiguous
    // regression signal.
    const EXPECTED = '75K';
    await expect(async () => {
      const navText = (await pExpCell.textContent())?.trim() ?? '';
      expect(navText).toContain(EXPECTED);
    }).toPass({ timeout: TIMEOUT });

    const navText = (await pExpCell.textContent())?.trim() ?? '';
    // Snapshot TOTAL row's Exp P&L cell — 3rd of the four populated
    // .tf-cell spans (day/pnl/exp/extrinsic); Extrinsic is empty for a
    // futures-only fixture (legExtrinsicDisplay is options-only), so
    // filtering to non-empty cells and taking the last one lands on Exp P&L.
    const totalExpCell = totalRow.locator('.num.tf-cell').filter({ hasText: /\S/ }).last();
    const snapText = (await totalExpCell.textContent())?.trim() ?? '';

    expect(navText, 'NavStrip P slot 3 (Exp P&L) must show the split-aware 75K total, not the naive 50K').toContain(EXPECTED);
    expect(snapText, 'Snapshot TOTAL Exp P&L must match NavStrip exactly (SSOT — same store-derived value)').toContain(EXPECTED);
  });
});
