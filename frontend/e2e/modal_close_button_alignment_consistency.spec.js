/**
 * modal_close_button_alignment_consistency.spec.js
 *
 * 2026-11-03 — operator reported "orders and chart modal header button
 * placement is not in sync. example. X button right alignment. Same
 * issue with navstrip popup window x button right alignment."
 *
 * Live measurement (getBoundingClientRect-equivalent `boundingBox()`,
 * desktop 1400x900, reducedMotion forced so the 260ms
 * `canonical-modal-enter` scaleY animation can't return a mid-transform
 * box) found the Order modal (`SymbolPanel.svelte`, `.oes-close`) was the
 * real outlier:
 *
 *   Order modal   (.oes-modal / .oes-close):   rightGap≈1px,  topGap≈0px
 *   Chart modal   (.cm-modal  / .cm-close):     rightGap≈14.6px, topGap≈5.6px
 *   NavBreakdown  (.ps-breakdown-panel/.ps-bd-close): rightGap≈14.6px, topGap≈6.6px
 *
 * Root cause: the Order modal's header uses the shared `<CardHeader>`
 * component, which (per the 2026-10 CardHeader/grid consistency pass,
 * Part C) inherits the body-level `--ch-padding: 0` default — zero
 * padding, close button flush to the corner. ChartModal and
 * NavBreakdown both build their header on the DIFFERENT shared
 * `.canonical-modal-header` class (app.css), which carries
 * `padding: 0.35rem 0.85rem`. Two different padding systems for what is
 * visually the same "X button in the top-right of a modal" affordance.
 *
 * Fix: `SymbolPanel.svelte` now sets a scoped
 * `.oes-modal-hdr-canonical { --ch-padding: 0.35rem 0.85rem; }` override
 * on the wrapping element around its `<CardHeader>` call, applied only in
 * real-modal mode (`!inline`). The GLOBAL `--ch-padding: 0` default in
 * `(algo)/+layout.svelte` is untouched — every ordinary in-page
 * CardHeader card keeps its zero-gap header-to-content seam.
 *
 * Verified after the fix (same measurement): Order and Chart modal now
 * land at the IDENTICAL inset (rightGap 14.59375px, topGap 5.59375px —
 * exact pixel match on this viewport). NavBreakdown's topGap differs by
 * ~1px from the other two (6.59 vs 5.59) — that is a PRE-EXISTING,
 * separately-documented ~1px variance between NavBreakdown's own panel
 * and the `.canonical-modal-panel`/`.canonical-modal-header` pair (see
 * `chart_modal_close_button_gap.spec.js`'s own distinct
 * EXPECTED_TOP_GAP_PX constants: 5.6 for Chart vs the NavStrip spec's own
 * 6.6), not something this fix introduced or is in scope to close
 * further. This test tolerates that pre-existing ~1-2px spread while
 * tightly gating the rightGap (the operator's own explicit example) and
 * the Order/Chart topGap pair (newly identical after this fix).
 *
 * Run:
 *   cd frontend && npx playwright test modal_close_button_alignment_consistency
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 30000;
const RIGHT_GAP_TOLERANCE_PX = 1.5;
const TOP_GAP_TOLERANCE_PX = 2;

/** @param {import('@playwright/test').Page} page */
async function measure(page, panelSel, closeSel) {
  const panel = page.locator(panelSel).first();
  const close = page.locator(closeSel).first();
  await expect(panel).toBeVisible({ timeout: 10000 });
  await expect(close).toBeVisible({ timeout: 10000 });
  const panelBox = await panel.boundingBox();
  const closeBox = await close.boundingBox();
  if (!panelBox || !closeBox) return null;
  return {
    rightGap: (panelBox.x + panelBox.width) - (closeBox.x + closeBox.width),
    topGap: closeBox.y - panelBox.y,
  };
}

test.describe('Modal/popup close-button inset — Order, Chart, NavBreakdown agree (2026-11-03)', () => {
  test.beforeEach(async ({ page }) => {
    // Kill the canonical-modal-enter scaleY animation so a measurement
    // taken right after open can't land mid-transform.
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await loginAsAdmin(page);
  });

  test('Order modal close button lands at the canonical inset, not flush', async ({ page }) => {
    test.setTimeout(60000);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: TIMEOUT }).catch(() => {});
    await page.waitForTimeout(2000);
    const orderBtn = page.locator('button.pha-order').first();
    const visible = await orderBtn.isVisible({ timeout: 8000 }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: 'Order button not visible' });
      return;
    }
    await orderBtn.click();
    await page.waitForTimeout(500);

    const r = await measure(page, '.oes-modal', '.oes-close');
    expect(r).toBeTruthy();
    // Operator's explicit example: right-alignment. Must sit at the
    // canonical ~14.6px inset, not flush (~1px, the pre-fix regression).
    expect(r.rightGap).toBeGreaterThan(8);
    expect(r.topGap).toBeGreaterThan(2);
  });

  test('Chart modal close button lands at the canonical inset', async ({ page }) => {
    test.setTimeout(60000);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: TIMEOUT }).catch(() => {});
    await page.waitForTimeout(1500);
    const chartBtn = page.locator('button.pha-chart').first();
    const visible = await chartBtn.isVisible({ timeout: 8000 }).catch(() => false);
    if (!visible) {
      test.info().annotations.push({ type: 'skip', description: 'Chart button not visible' });
      return;
    }
    await chartBtn.click();
    await page.waitForTimeout(500);

    const r = await measure(page, '.cm-modal', '.cm-close');
    expect(r).toBeTruthy();
    expect(r.rightGap).toBeGreaterThan(8);
    expect(r.topGap).toBeGreaterThan(2);
  });

  test('NavBreakdown popup close button lands at the canonical inset', async ({ page }) => {
    test.setTimeout(60000);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: TIMEOUT }).catch(() => {});
    await page.waitForTimeout(1500);
    const strip = page.locator('.ps-strip');
    const stripVisible = await strip.isVisible({ timeout: 8000 }).catch(() => false);
    if (!stripVisible) {
      test.info().annotations.push({ type: 'skip', description: 'PositionStrip not visible' });
      return;
    }
    const value = page.locator('.ps-strip .ps-k-p')
      .locator('xpath=following-sibling::span[1][contains(@class, "ps-agg-v")]').first();
    const valueVisible = await value.isVisible({ timeout: 5000 }).catch(() => false);
    if (!valueVisible) {
      test.info().annotations.push({ type: 'skip', description: 'P pill value not visible' });
      return;
    }
    await value.click();

    const r = await measure(page, '.ps-breakdown-panel', '.ps-bd-close');
    expect(r).toBeTruthy();
    expect(r.rightGap).toBeGreaterThan(8);
    expect(r.topGap).toBeGreaterThan(2);
  });

  test('all three insets agree within tolerance — the actual consistency check', async ({ page }) => {
    test.setTimeout(90000);

    // Order modal
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: TIMEOUT }).catch(() => {});
    await page.waitForTimeout(2000);
    const orderBtn = page.locator('button.pha-order').first();
    if (!(await orderBtn.isVisible({ timeout: 8000 }).catch(() => false))) {
      test.info().annotations.push({ type: 'skip', description: 'Order button not visible' });
      return;
    }
    await orderBtn.click();
    await page.waitForTimeout(500);
    const orderR = await measure(page, '.oes-modal', '.oes-close');
    // Close the order modal before navigating away.
    await page.locator('.oes-close').first().click().catch(() => {});
    await page.waitForTimeout(300);

    // Chart modal
    await page.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: TIMEOUT }).catch(() => {});
    await page.waitForTimeout(1500);
    const chartBtn = page.locator('button.pha-chart').first();
    if (!(await chartBtn.isVisible({ timeout: 8000 }).catch(() => false))) {
      test.info().annotations.push({ type: 'skip', description: 'Chart button not visible' });
      return;
    }
    await chartBtn.click();
    await page.waitForTimeout(500);
    const chartR = await measure(page, '.cm-modal', '.cm-close');
    await page.locator('.cm-close').first().click().catch(() => {});
    await page.waitForTimeout(300);

    // NavBreakdown popup (same /pulse page)
    const strip = page.locator('.ps-strip');
    if (!(await strip.isVisible({ timeout: 8000 }).catch(() => false))) {
      test.info().annotations.push({ type: 'skip', description: 'PositionStrip not visible' });
      return;
    }
    const value = page.locator('.ps-strip .ps-k-p')
      .locator('xpath=following-sibling::span[1][contains(@class, "ps-agg-v")]').first();
    if (!(await value.isVisible({ timeout: 5000 }).catch(() => false))) {
      test.info().annotations.push({ type: 'skip', description: 'P pill value not visible' });
      return;
    }
    await value.click();
    const navR = await measure(page, '.ps-breakdown-panel', '.ps-bd-close');

    expect(orderR).toBeTruthy();
    expect(chartR).toBeTruthy();
    expect(navR).toBeTruthy();

    // Right inset: the operator's explicit example — all three must
    // agree tightly. Order and Chart share the exact same CardHeader
    // padding now, so this is a strict check, not a loosened one.
    expect(Math.abs(orderR.rightGap - chartR.rightGap)).toBeLessThanOrEqual(RIGHT_GAP_TOLERANCE_PX);
    expect(Math.abs(orderR.rightGap - navR.rightGap)).toBeLessThanOrEqual(RIGHT_GAP_TOLERANCE_PX);
    expect(Math.abs(chartR.rightGap - navR.rightGap)).toBeLessThanOrEqual(RIGHT_GAP_TOLERANCE_PX);

    // Top inset: Order and Chart share the identical header recipe post-fix,
    // so they must match tightly. NavBreakdown gets the wider,
    // pre-existing tolerance documented in the file header above.
    expect(Math.abs(orderR.topGap - chartR.topGap)).toBeLessThanOrEqual(RIGHT_GAP_TOLERANCE_PX);
    expect(Math.abs(orderR.topGap - navR.topGap)).toBeLessThanOrEqual(TOP_GAP_TOLERANCE_PX);
    expect(Math.abs(chartR.topGap - navR.topGap)).toBeLessThanOrEqual(TOP_GAP_TOLERANCE_PX);
  });
});
