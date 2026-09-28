/**
 * legs_grid_qty_color_scoping.spec.js
 *
 * Regression guard for a Svelte scoped-CSS bug: `.kv-pos`/`.kv-neg`
 * (Qty column long/short color) and `.cell-muted` (O/N Qty column) were
 * only ever defined in the PARENT (+page.svelte)'s <style> block, for
 * its OWN direct markup — Svelte's scoped CSS is per-component, so a
 * rule defined in the parent never reaches elements rendered inside a
 * CHILD component's own template (CandidateLegRow.svelte here). Both
 * classes rendered with no color at all in the derivatives Legs grid.
 * `.cell-pos`/`.cell-neg`/`.cell-flat` were unaffected — those three
 * are ALSO defined globally in app.css, so they worked regardless of
 * scoping; `.kv-pos`/`.kv-neg`/`.cell-muted` have no such fallback.
 *
 * Fix: define `.kv-pos`/`.kv-neg`/`.cell-muted` in
 * CandidateLegRow.svelte's own <style> block too (same color tokens as
 * the parent's definitions).
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *     npx playwright test e2e/legs_grid_qty_color_scoping.spec.js \
 *     --project=chromium-desktop
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

test('Legs grid Qty and O/N Qty cells are colored, not the muted/default text color', async ({ page }) => {
  test.setTimeout(60_000);
  await loginAsAdmin(page);
  await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded', timeout: 30_000 });
  await page.waitForTimeout(2500);

  const legRows = page.locator('.cand-row:not(.cand-row-total)');
  const rowCount = await legRows.count();
  if (rowCount === 0) {
    test.skip(true, 'no live F&O positions to render Legs rows — skip');
    return;
  }

  // Qty column: 5th direct-child span in the row (state, symbol, ltp,
  // chg%, then Qty) carrying class kv-pos or kv-neg.
  const qtyCell = legRows.first().locator('.kv-pos, .kv-neg').first();
  if (await qtyCell.count() === 0) {
    test.skip(true, 'no .kv-pos/.kv-neg cell rendered (e.g. isClosed row) — skip');
    return;
  }
  const qtyColor = await qtyCell.evaluate((el) => getComputedStyle(el).color);
  // Default/unstyled text on this dark theme renders as a slate/white
  // shade, not the long (green) / short (red) accent — assert it's
  // neither transparent/unset NOR the plain default slate color.
  expect(qtyColor).not.toBe('rgba(0, 0, 0, 0)');
  const defaultSlate = await legRows.first().evaluate((el) => getComputedStyle(el).color);
  expect(
    qtyColor,
    `Qty cell color (${qtyColor}) must differ from the row's plain default text color (${defaultSlate}) — .kv-pos/.kv-neg must be genuinely colored, not falling through unstyled`
  ).not.toBe(defaultSlate);

  // O/N Qty column: .cell-muted, always rendered (dash or a value).
  const onQtyCell = legRows.first().locator('.cell-muted').first();
  await expect(onQtyCell).toBeVisible({ timeout: 5000 });
  const onQtyColor = await onQtyCell.evaluate((el) => getComputedStyle(el).color);
  expect(
    onQtyColor,
    `O/N Qty cell color (${onQtyColor}) must differ from the row's plain default text color (${defaultSlate}) — .cell-muted must be genuinely dimmed, not falling through unstyled`
  ).not.toBe(defaultSlate);
});
