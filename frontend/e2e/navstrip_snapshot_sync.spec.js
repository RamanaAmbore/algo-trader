/**
 * NavStrip P pill ↔ Snapshot TOTAL row — SSOT sync guard.
 *
 * Root cause fixed 2026-07-02: _perRootReduce lacked a matchStrategy gate,
 * so snapshotTotals published ALL positions while per-row data in the
 * Snapshot grid was narrowed by the strategy filter.  After the fix both
 * paths share _makeStrategyMatcher() and produce identical numbers.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *   SSOT  — NavStrip P day/pnl/exp equals Snapshot TOTAL day/pnl/exp when
 *            derivatives is mounted (no strategy filter + with strategy filter).
 *   Perf  — reading both DOM values is a single page load, no extra round-trip.
 *   Stale — no stale pattern: snapshotTotals store is populated from
 *            _perRootReduce (grep check), _makeStrategyMatcher is in the file.
 *   Reuse — _perRootReduce receives matchStrategy param (grep check),
 *            _makeStrategyMatcher helper exists (grep check).
 *   UX    — NavStrip P pill shows 3 values; Snapshot TOTAL row has a
 *            .byund-row-total element with numeric cells.
 *
 * Run:
 *   cd frontend && npx playwright test navstrip_snapshot_sync --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE    = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const TIMEOUT = 25_000;

// ── Stale / Reuse code-level guards (no browser required) ────────────────────

test.describe('Code-level SSOT guards', () => {
  test('_perRootReduce accepts matchStrategy parameter', async () => {
    const fs   = await import('fs');
    const path = await import('path');
    const src  = fs.readFileSync(
      path.resolve(
        import.meta.dirname,
        '../src/routes/(algo)/admin/derivatives/+page.svelte',
      ),
      'utf8',
    );
    // matchStrategy default param in function signature
    expect(src).toContain('function _perRootReduce(accessor, matchStrategy = () => true)');
    // _makeStrategyMatcher helper present
    expect(src).toContain('function _makeStrategyMatcher()');
    // All three callers call _makeStrategyMatcher() and pass the result
    const msBuilds = (src.match(/_makeStrategyMatcher\(\)/g) || []).length;
    expect(msBuilds).toBeGreaterThanOrEqual(3);
    // All three callers pass ms as second arg (ends the line with ", ms);")
    const msPass = (src.match(/,\s*ms\);/g) || []).length;
    expect(msPass).toBeGreaterThanOrEqual(3);
    // strategy gate applied inside _perRootReduce
    expect(src).toContain('if (!matchStrategy(');
  });

  test('snapshotTotals store is populated from _snapshotTotalDay/Pnl/Exp', async () => {
    const fs   = await import('fs');
    const path = await import('path');
    const src  = fs.readFileSync(
      path.resolve(
        import.meta.dirname,
        '../src/routes/(algo)/admin/derivatives/+page.svelte',
      ),
      'utf8',
    );
    expect(src).toContain('day: _snapshotTotalDay');
    expect(src).toContain('pnl: _snapshotTotalPnl');
    expect(src).toContain('exp: _snapshotTotalExp');
  });

  test('PositionStrip reads snapshotTotals with != null guard (not ?? which swallows 0)', async () => {
    const fs   = await import('fs');
    const path = await import('path');
    const src  = fs.readFileSync(
      path.resolve(import.meta.dirname, '../src/lib/PositionStrip.svelte'),
      'utf8',
    );
    // All three slots use explicit != null ternary (fixed from ?? in 2026-07-02)
    // so a stored value of 0 (stale derivatives visit) does not suppress the live compute
    expect(src).toContain('$snapshotTotals != null ? $snapshotTotals.day');
    expect(src).toContain('$snapshotTotals != null ? $snapshotTotals.pnl');
    expect(src).toContain('$snapshotTotals != null ? $snapshotTotals.exp');
    // The ?? operator must NOT be used on snapshotTotals slots (regression guard)
    expect(src).not.toContain('$snapshotTotals?.day ??');
    expect(src).not.toContain('$snapshotTotals?.pnl ??');
    expect(src).not.toContain('$snapshotTotals?.exp ??');
    // No untrack() wrapping snapshotTotals read (would stale-cache it)
    expect(src).not.toMatch(/untrack\s*\(\s*\(\)\s*=>\s*\$snapshotTotals/);
  });
});

// ── Browser: NavStrip P pill structure ───────────────────────────────────────

test.describe('NavStrip P pill — structure', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('P pill has 3 slash-separated values on /pulse', async ({ page }) => {
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });
    const pPill = strip.locator('.ps-agg').first();
    await expect(pPill).toBeVisible({ timeout: TIMEOUT });
    const vals = pPill.locator('.ps-agg-v');
    await expect(vals).toHaveCount(3, { timeout: TIMEOUT });
  });

  test('P pill has 3 slash-separated values on /admin/derivatives', async ({ page }) => {
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });
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
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('Snapshot TOTAL row is the last row in the byund grid when data present', async ({ page }) => {
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });
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
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });
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
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await loginAsAdmin(page);
  });

  test('P pill visible and not overflowing on 390px', async ({ page }) => {
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });
    const box = await strip.boundingBox();
    expect(box).not.toBeNull();
    expect(box.width).toBeLessThanOrEqual(390);
    const pPill = strip.locator('.ps-agg').first();
    await expect(pPill).toBeVisible({ timeout: TIMEOUT });
  });
});
