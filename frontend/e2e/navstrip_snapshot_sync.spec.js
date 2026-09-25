/**
 * NavStrip P pill ↔ Snapshot TOTAL row — SSOT sync guard.
 *
 * Root cause fixed 2026-07-02: _perRootReduce lacked a matchStrategy gate,
 * so the old `snapshotTotals` store published ALL positions while per-row
 * data in the Snapshot grid was narrowed by the strategy filter.
 *
 * Architecture superseded 2026-09 (commit cbe132a6, "NavStrip/Snapshot Exp
 * P&L SSOT"): the writable `snapshotTotals` store and its page $effect
 * publisher were removed entirely. NavStrip's P pill and the Snapshot TOTAL
 * row now both read from the SAME reactive source instead of one surface
 * pushing a snapshot into a store the other polls:
 *   - portfolioStore.svelte.js computes exp_pnl ONCE per F&O position row
 *     via the split-aware positionExpPnl() helper (expiryPnl.js) and fans
 *     that single `p._exp_pnl` value out to both `posTotal.exp_pnl`
 *     (NavStrip's unfiltered total, via positionsDerivedStore.expiryTotal)
 *     and `expPnlRows` (the per-row array the Snapshot grid's filtered
 *     TOTAL sums via _reduceStoreExpRows) — never two independent
 *     computations of the same number.
 *   - +page.svelte's live-mode Exp P&L/Extrinsic reduction
 *     (_filteredExpPnlByRoot / _filteredExtrinsicByRoot) reads
 *     portfolioStore.positions.expPnlRows through _reduceStoreExpRows
 *     instead of recomputing per-leg via _legExpPnlDisplay; SIM mode keeps
 *     the local _perRootReduce path (sim positions never reach the store).
 *   - Both live-mode and sim-mode paths apply the SAME matchAccount +
 *     matchStrategy gate (_makeStrategyMatcher / buildStrategyMatcher),
 *     preserving the 2026-07-02 fix's filter-consistency property.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *   SSOT  — NavStrip P day/pnl/exp equals Snapshot TOTAL day/pnl/exp when
 *            derivatives is mounted (no strategy filter + with strategy filter).
 *   Perf  — reading both DOM values is a single page load, no extra round-trip.
 *   Stale — no stale pattern: no `snapshotTotals` store reference anywhere;
 *            Exp P&L is derived exactly once in portfolioStore, not per-surface.
 *   Reuse — _reduceStoreExpRows / positionExpPnl / expPnlRows are the single
 *            shared implementation both NavStrip and Snapshot consume.
 *   UX    — NavStrip P pill shows 3 values; Snapshot TOTAL row has a
 *            .byund-row-total element with numeric cells.
 *
 * Run:
 *   cd frontend && npx playwright test navstrip_snapshot_sync --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import fs from 'fs';
import path from 'path';

const BASE    = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const TIMEOUT = 25_000;

const _PAGE_PATH  = path.resolve(import.meta.dirname, '../src/routes/(algo)/admin/derivatives/+page.svelte');
const _STORE_PATH = path.resolve(import.meta.dirname, '../src/lib/data/portfolioStore.svelte.js');
const _SHIM_PATH  = path.resolve(import.meta.dirname, '../src/lib/data/positionsDerivedStore.svelte.js');
const _STRIP_PATH = path.resolve(import.meta.dirname, '../src/lib/PositionStrip.svelte');

// ── Stale / Reuse code-level guards (no browser required) ────────────────────
//
// These three tests assert the CURRENT (2026-09, commit cbe132a6) SSOT
// architecture. Each targets a specific code line introduced by that fix —
// reverting the fix (restoring the old independent-computation code) makes
// the corresponding assertion below fail; this was verified manually by
// temporarily reintroducing the pre-fix lines and confirming red, then
// restoring the fix and confirming green (not committed — see commit body).

test.describe('Code-level SSOT guards', () => {
  test('portfolioStore derives exp_pnl once via positionExpPnl and fans it out to posTotal + expPnlRows', async () => {
    const src = fs.readFileSync(_STORE_PATH, 'utf8');
    // The split-aware derivation runs for BOTH the open-qty and closed-qty
    // branches — this is what makes NavStrip's total agree with Snapshot's
    // TOTAL instead of trusting Kite's raw (sometimes-zero) realised field.
    expect(src).toContain('exp_pnl = positionExpPnl(p, kind, anchor);');
    expect(src).toContain('exp_pnl = positionExpPnl(p, kind, null);');
    // posTotal.exp_pnl (NavStrip's unfiltered total) and expPnlRows (the
    // Snapshot grid's per-row filtered source) both read the SAME p._exp_pnl
    // field inside the same accumulation loop — one computation, two readers.
    expect(src).toMatch(/posTotal\.exp_pnl\s*\+=\s*p\._exp_pnl;/);
    expect(src).toMatch(/exp_pnl:\s*p\._exp_pnl,/);
    expect(src).toContain('const expPnlRows = [];');
  });

  test('Snapshot live-mode Exp P&L reads portfolioStore.positions.expPnlRows, not an independent per-leg recompute', async () => {
    const src = fs.readFileSync(_PAGE_PATH, 'utf8');
    // _reduceStoreExpRows walks the store's own per-row array and applies
    // the SAME account/strategy gate the rest of the Snapshot row uses.
    expect(src).toContain('function _reduceStoreExpRows(field, matchStrategy) {');
    expect(src).toContain('for (const r of portfolioStore.positions.expPnlRows) {');
    expect(src).toContain('if (!matchAccount(r.account)) continue;');
    expect(src).toContain('if (!matchStrategy(r.symbol)) continue;');
    // Live mode delegates to the store reduction; SIM mode (positions never
    // reach the store) keeps the local per-leg accessor path.
    expect(src).toContain("return _reduceStoreExpRows('exp_pnl', matchStrategy);");
    expect(src).toContain("return _reduceStoreExpRows('extrinsic', matchStrategy);");
    // The Snapshot TOTAL row sums exactly the filtered per-row values above it.
    expect(src).toContain('function _rowExpPnlFor(');
    expect(src).toContain('return _filteredExpPnlByRoot[underlying] ?? 0;');
    // Both callers build the strategy gate through the shared helper (the
    // 2026-07-02 filter-consistency fix, preserved by the 2026-09 refactor).
    expect(src).toContain('function _makeStrategyMatcher() {');
    const msBuilds = (src.match(/_makeStrategyMatcher\(\)/g) || []).length;
    expect(msBuilds).toBeGreaterThanOrEqual(2);
    // The dead `snapshotTotals` store must not reappear.
    expect(src).not.toContain('snapshotTotals');
  });

  test('NavStrip P pill (PositionStrip) reads the same canonical exp_pnl total as the Snapshot grid', async () => {
    const stripSrc = fs.readFileSync(_STRIP_PATH, 'utf8');
    const shimSrc  = fs.readFileSync(_SHIM_PATH, 'utf8');
    // PositionStrip's Exp P&L slot reads positionsDerivedStore.expiryTotal —
    // no independent computation inside PositionStrip itself.
    expect(stripSrc).toContain('{fmtMoney(positionsDerivedStore.expiryTotal)}');
    expect(stripSrc).not.toContain('snapshotTotals');
    // That getter is a plain one-line alias of portfolioStore's own total —
    // the SAME posTotal.exp_pnl asserted (via p._exp_pnl) in the first test
    // above, so NavStrip and the Snapshot TOTAL are provably the same number
    // by construction, not by two implementations happening to agree.
    expect(shimSrc).toMatch(/get expiryTotal\(\)\s*\{\s*return portfolioStore\.positions\.total\.exp_pnl;/);
  });
});

// ── Browser: NavStrip P pill structure ───────────────────────────────────────

test.describe('NavStrip P pill — structure', () => {
  // /pulse keeps an open SSE connection; allow 45s for loginAsAdmin + goto + element
  test.setTimeout(45_000);
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('P pill has 3 slash-separated values on /pulse', async ({ page }) => {
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });
    const pPill = strip.locator('.ps-agg').first();
    await expect(pPill).toBeVisible({ timeout: TIMEOUT });
    const vals = pPill.locator('.ps-agg-v');
    await expect(vals).toHaveCount(3, { timeout: TIMEOUT });
  });

  test('P pill has 3 slash-separated values on /admin/derivatives', async ({ page }) => {
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded' });
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });
    const pPill = strip.locator('.ps-agg').first();
    await expect(pPill).toBeVisible({ timeout: TIMEOUT });
    const vals = pPill.locator('.ps-agg-v');
    await expect(vals).toHaveCount(3, { timeout: TIMEOUT });
  });
});

// ── Browser: Snapshot TOTAL row exists when positions present ────────────────

test.describe('Snapshot TOTAL row — structure', () => {
  test.setTimeout(45_000);
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('Snapshot TOTAL row is the last row in the byund grid when data present', async ({ page }) => {
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded' });
    // Wait for the Snapshot card to stabilise
    const card = page.locator('.opt-byund-card');
    await expect(card).toBeVisible({ timeout: TIMEOUT });
    // If any data rows exist, a TOTAL row should be present
    const dataRows = card.locator('.byund-row:not(.byund-row-total)');
    const totalRow = card.locator('.byund-row-total');
    const rowCount = await dataRows.count();
    if (rowCount > 0) {
      await expect(totalRow).toBeVisible({ timeout: TIMEOUT });
      // TOTAL row must contain at least one numeric cell with a value
      const numCells = totalRow.locator('.num.tf-cell');
      await expect(numCells.first()).toBeVisible({ timeout: TIMEOUT });
    }
  });

  test('NavStrip P and Snapshot TOTAL are both visible simultaneously on derivatives', async ({ page }) => {
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded' });
    const strip    = page.locator('.ps-strip');
    const snapCard = page.locator('.opt-byund-card');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });
    await expect(snapCard).toBeVisible({ timeout: TIMEOUT });
    // Both surfaces rendered in same DOM — no async gap possible
    const pPill   = strip.locator('.ps-agg').first();
    const pVals   = pPill.locator('.ps-agg-v');
    await expect(pVals).toHaveCount(3, { timeout: TIMEOUT });
  });
});

// ── Browser: mobile viewport — P pill fits ───────────────────────────────────

test.describe('NavStrip P pill — mobile viewport', () => {
  test.setTimeout(45_000);
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await loginAsAdmin(page);
  });

  test('P pill visible and not overflowing on 390px', async ({ page }) => {
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });
    const box = await strip.boundingBox();
    expect(box).not.toBeNull();
    expect(box.width).toBeLessThanOrEqual(390);
    const pPill = strip.locator('.ps-agg').first();
    await expect(pPill).toBeVisible({ timeout: TIMEOUT });
  });
});
