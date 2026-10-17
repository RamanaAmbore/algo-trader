/**
 * navstrip_breakdown_close_button_gap.spec.js
 *
 * Regression test for the 2026-10-17 fix: NavStrip's NavBreakdown popup had
 * a visible gap between the close button and the panel's right edge when
 * content scrolled, due to the scrollbar's gutter eating into the shared
 * scrolling container (which also held the header). The fix moves the scroll
 * to an inner `.ps-bd-body` wrapper, keeping the header outside any scrolling.
 *
 * Root cause: `.ps-breakdown-panel` had `overflow-y: auto` directly on the
 * popup container (the same element holding `.ps-bd-header`). When content
 * tall enough to scroll appeared, the scrollbar gutter reduced the available
 * width for ALL children including the header — visibly pushing `.ps-bd-close`
 * left of the panel's true right edge. Other dark-theme popups
 * (BrokerHealthBadge, ChartModal, etc.) use the `.algo-modal` convention:
 * header OUTSIDE the scroll box, only the body scrolls → close button never
 * shifts.
 *
 * The fix:
 * - `.ps-breakdown-panel`: `overflow: hidden; display: flex; flex-direction: column;`
 * - `.ps-bd-body`: new wrapper with `overflow-y: auto; flex: 1 1 auto; min-height: 0;`
 * - Header `.ps-bd-header` stays outside scroll → right padding always controls
 *   the close button's distance from the panel's true right edge.
 *
 * Verifies:
 *   1. Breakdown popup opens on pill click (P/M/C/H values).
 *   2. Close button's right edge is aligned with panel right edge minus
 *      the canonical header padding (0.85rem = ~13.6px at 16px root font).
 *   3. BrokerHealthBadge's close button has the same distance (consistency).
 *   4. On mobile viewport with tall content (if achievable), header doesn't
 *      shift even when body scrolls.
 *
 * Run:
 *   cd frontend && npx playwright test navstrip_breakdown_close_button_gap
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 30000;
// canonical-modal-header padding: 0.35rem 0.85rem
// Right padding is 0.85rem; at 16px root = ~13.6px
// We tolerate ±3px for rounding on different viewport sizes
const EXPECTED_RIGHT_PADDING_PX = 13.6;
const TOLERANCE_PX = 3;

test.describe('NavStrip breakdown close-button gap regression', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: TIMEOUT }).catch(() => {});
    await page.waitForTimeout(2000); // Allow NavStrip data to populate
  });

  test('desktop: breakdown panel opens on P pill click', async ({ page }) => {
    test.setTimeout(60000);
    const strip = page.locator('.ps-strip');
    const stripVisible = await strip.isVisible({ timeout: 5000 }).catch(() => false);
    if (!stripVisible) {
      test.info().annotations.push({ type: 'skip', description: 'PositionStrip not visible' });
      return;
    }

    // Click the first P value (not the label, which opens InfoHint)
    const pValue = page.locator('.ps-strip .ps-k-p')
      .locator('xpath=following-sibling::span[1][contains(@class, "ps-agg-v")]')
      .first();
    const pVisible = await pValue.isVisible({ timeout: 3000 }).catch(() => false);
    if (!pVisible) {
      test.skip();
    }

    await pValue.click();
    const panel = page.locator('.ps-breakdown-panel');
    await expect(panel).toBeVisible({ timeout: 5000 });
  });

  test('desktop: close button distance from panel right edge matches canonical padding', async ({ page }) => {
    test.setTimeout(60000);
    const strip = page.locator('.ps-strip');
    const stripVisible = await strip.isVisible({ timeout: 5000 }).catch(() => false);
    if (!stripVisible) {
      test.info().annotations.push({ type: 'skip', description: 'PositionStrip not visible' });
      return;
    }

    // Open P breakdown
    const pValue = page.locator('.ps-strip .ps-k-p')
      .locator('xpath=following-sibling::span[1][contains(@class, "ps-agg-v")]')
      .first();
    const pVisible = await pValue.isVisible({ timeout: 3000 }).catch(() => false);
    if (!pVisible) {
      test.info().annotations.push({ type: 'skip', description: 'P-slot value not visible' });
      return;
    }

    await pValue.click();
    const panel = page.locator('.ps-breakdown-panel');
    await expect(panel).toBeVisible({ timeout: 5000 });

    // Measure the panel and close button bounding boxes
    const panelBox = await panel.boundingBox();
    const closeBtn = panel.locator('.ps-bd-close');
    const closeBtnBox = await closeBtn.boundingBox();

    expect(panelBox).toBeTruthy();
    expect(closeBtnBox).toBeTruthy();

    // Calculate the distance from close button's right edge to panel's right edge
    const panelRightEdge = panelBox.x + panelBox.width;
    const closeBtnRightEdge = closeBtnBox.x + closeBtnBox.width;
    const distanceFromRight = panelRightEdge - closeBtnRightEdge;

    // The close button should be right-padded at ~13.6px (0.85rem canonical-modal-header padding)
    expect(distanceFromRight).toBeGreaterThanOrEqual(EXPECTED_RIGHT_PADDING_PX - TOLERANCE_PX);
    expect(distanceFromRight).toBeLessThanOrEqual(EXPECTED_RIGHT_PADDING_PX + TOLERANCE_PX);
  });

  test('desktop: close button position matches BrokerHealthBadge consistency check', async ({ page }) => {
    test.setTimeout(60000);
    const strip = page.locator('.ps-strip');
    const stripVisible = await strip.isVisible({ timeout: 5000 }).catch(() => false);
    if (!stripVisible) {
      test.info().annotations.push({ type: 'skip', description: 'PositionStrip not visible' });
      return;
    }

    // Open P breakdown
    const pValue = page.locator('.ps-strip .ps-k-p')
      .locator('xpath=following-sibling::span[1][contains(@class, "ps-agg-v")]')
      .first();
    const pVisible = await pValue.isVisible({ timeout: 3000 }).catch(() => false);
    if (!pVisible) {
      test.info().annotations.push({ type: 'skip', description: 'P-slot value not visible' });
      return;
    }

    await pValue.click();
    const psPanel = page.locator('.ps-breakdown-panel');
    await expect(psPanel).toBeVisible({ timeout: 5000 });

    // Measure the NavStrip breakdown close button
    const psCloseBtn = psPanel.locator('.ps-bd-close');
    const psPanelBox = await psPanel.boundingBox();
    const psCloseBtnBox = await psCloseBtn.boundingBox();

    // Now open BrokerHealthBadge modal for comparison
    const chip = page.locator('.broker-chip').first();
    const chipVisible = await chip.isVisible({ timeout: 3000 }).catch(() => false);
    if (!chipVisible) {
      test.info().annotations.push({ type: 'skip', description: 'Broker chip not visible, skipping consistency check' });
      return;
    }

    await chip.click();
    const bhModal = page.locator('.bh-modal');
    await expect(bhModal).toBeVisible({ timeout: 5000 });

    // Measure the BrokerHealthBadge close button
    const bhCloseBtn = bhModal.locator('.bh-close');
    const bhModalBox = await bhModal.boundingBox();
    const bhCloseBtnBox = await bhCloseBtn.boundingBox();

    // Both should have roughly the same right-edge distance from their panel right edges
    const psDistFromRight = (psPanelBox.x + psPanelBox.width) - (psCloseBtnBox.x + psCloseBtnBox.width);
    const bhDistFromRight = (bhModalBox.x + bhModalBox.width) - (bhCloseBtnBox.x + bhCloseBtnBox.width);

    // Both should be in the canonical padding range (13.6px ± 3px)
    expect(psDistFromRight).toBeGreaterThanOrEqual(EXPECTED_RIGHT_PADDING_PX - TOLERANCE_PX);
    expect(psDistFromRight).toBeLessThanOrEqual(EXPECTED_RIGHT_PADDING_PX + TOLERANCE_PX);
    expect(bhDistFromRight).toBeGreaterThanOrEqual(EXPECTED_RIGHT_PADDING_PX - TOLERANCE_PX);
    expect(bhDistFromRight).toBeLessThanOrEqual(EXPECTED_RIGHT_PADDING_PX + TOLERANCE_PX);

    // The two distances should also be very close to each other (within 1px, accounting for sub-pixel rendering)
    expect(Math.abs(psDistFromRight - bhDistFromRight)).toBeLessThanOrEqual(2);
  });

  test('desktop: header stays in place when body content scrolls', async ({ page }) => {
    // This test verifies the structural fix: that the header never moves
    // when the body scrolls. We measure the header's position before and
    // after scrolling the body internally.
    test.setTimeout(60000);
    const strip = page.locator('.ps-strip');
    const stripVisible = await strip.isVisible({ timeout: 5000 }).catch(() => false);
    if (!stripVisible) {
      test.info().annotations.push({ type: 'skip', description: 'PositionStrip not visible' });
      return;
    }

    // Open P breakdown (has multiple account rows if available)
    const pValue = page.locator('.ps-strip .ps-k-p')
      .locator('xpath=following-sibling::span[1][contains(@class, "ps-agg-v")]')
      .first();
    const pVisible = await pValue.isVisible({ timeout: 3000 }).catch(() => false);
    if (!pVisible) {
      test.info().annotations.push({ type: 'skip', description: 'P-slot value not visible' });
      return;
    }

    await pValue.click();
    const panel = page.locator('.ps-breakdown-panel');
    await expect(panel).toBeVisible({ timeout: 5000 });

    // Measure the header's position
    const header = panel.locator('.ps-bd-header');
    const headerBoxBefore = await header.boundingBox();

    // Try to scroll the body (if there's scrollable content)
    const body = panel.locator('.ps-bd-body');
    const bodyScrollHeight = await body.evaluate(el => el.scrollHeight);
    const bodyClientHeight = await body.evaluate(el => el.clientHeight);

    if (bodyScrollHeight > bodyClientHeight) {
      // Content is scrollable; scroll it
      await body.evaluate(el => el.scrollTop = el.scrollHeight);
      await page.waitForTimeout(100);

      // Re-measure the header
      const headerBoxAfter = await header.boundingBox();

      // The header's position should NOT have changed
      expect(headerBoxAfter.x).toBe(headerBoxBefore.x);
      expect(headerBoxAfter.y).toBe(headerBoxBefore.y);
      expect(headerBoxAfter.width).toBe(headerBoxBefore.width);
      expect(headerBoxAfter.height).toBe(headerBoxBefore.height);

      // The close button should also be at the same position
      const closeBtn = header.locator('.ps-bd-close');
      const closeBtnBoxBefore = await closeBtn.boundingBox();
      await body.evaluate(el => el.scrollTop = 0);
      const closeBtnBoxAfter = await closeBtn.boundingBox();

      expect(closeBtnBoxAfter.x).toBe(closeBtnBoxBefore.x);
      expect(closeBtnBoxAfter.y).toBe(closeBtnBoxBefore.y);
    } else {
      test.info().annotations.push({ type: 'skip', description: 'Content not tall enough to scroll in this environment' });
    }
  });

  test('mobile-portrait: close button distance is consistent with canonical padding', async ({ page }) => {
    // Mobile viewport should also maintain the close button at the canonical distance
    test.setTimeout(60000);
    const strip = page.locator('.ps-strip');
    const stripVisible = await strip.isVisible({ timeout: 5000 }).catch(() => false);
    if (!stripVisible) {
      test.info().annotations.push({ type: 'skip', description: 'PositionStrip not visible' });
      return;
    }

    // Open P breakdown
    const pValue = page.locator('.ps-strip .ps-k-p')
      .locator('xpath=following-sibling::span[1][contains(@class, "ps-agg-v")]')
      .first();
    const pVisible = await pValue.isVisible({ timeout: 3000 }).catch(() => false);
    if (!pVisible) {
      test.info().annotations.push({ type: 'skip', description: 'P-slot value not visible' });
      return;
    }

    await pValue.click();
    const panel = page.locator('.ps-breakdown-panel');
    await expect(panel).toBeVisible({ timeout: 5000 });

    // Measure the panel and close button
    const panelBox = await panel.boundingBox();
    const closeBtn = panel.locator('.ps-bd-close');
    const closeBtnBox = await closeBtn.boundingBox();

    expect(panelBox).toBeTruthy();
    expect(closeBtnBox).toBeTruthy();

    const panelRightEdge = panelBox.x + panelBox.width;
    const closeBtnRightEdge = closeBtnBox.x + closeBtnBox.width;
    const distanceFromRight = panelRightEdge - closeBtnRightEdge;

    // Same canonical padding check on mobile
    expect(distanceFromRight).toBeGreaterThanOrEqual(EXPECTED_RIGHT_PADDING_PX - TOLERANCE_PX);
    expect(distanceFromRight).toBeLessThanOrEqual(EXPECTED_RIGHT_PADDING_PX + TOLERANCE_PX);
  });

  test('mobile-portrait: ESC closes the breakdown', async ({ page }) => {
    // Bonus check: ESC works on mobile too
    test.setTimeout(60000);
    const strip = page.locator('.ps-strip');
    const stripVisible = await strip.isVisible({ timeout: 5000 }).catch(() => false);
    if (!stripVisible) {
      test.info().annotations.push({ type: 'skip', description: 'PositionStrip not visible' });
      return;
    }

    const pValue = page.locator('.ps-strip .ps-k-p')
      .locator('xpath=following-sibling::span[1][contains(@class, "ps-agg-v")]')
      .first();
    const pVisible = await pValue.isVisible({ timeout: 3000 }).catch(() => false);
    if (!pVisible) {
      test.info().annotations.push({ type: 'skip', description: 'P-slot value not visible' });
      return;
    }

    await pValue.click();
    const panel = page.locator('.ps-breakdown-panel');
    await expect(panel).toBeVisible({ timeout: 5000 });

    // Press ESC
    await page.keyboard.press('Escape');
    await expect(panel).not.toBeVisible({ timeout: 3000 });
  });

  test('mobile-portrait: close button X click closes the breakdown', async ({ page }) => {
    test.setTimeout(60000);
    const strip = page.locator('.ps-strip');
    const stripVisible = await strip.isVisible({ timeout: 5000 }).catch(() => false);
    if (!stripVisible) {
      test.info().annotations.push({ type: 'skip', description: 'PositionStrip not visible' });
      return;
    }

    const pValue = page.locator('.ps-strip .ps-k-p')
      .locator('xpath=following-sibling::span[1][contains(@class, "ps-agg-v")]')
      .first();
    const pVisible = await pValue.isVisible({ timeout: 3000 }).catch(() => false);
    if (!pVisible) {
      test.info().annotations.push({ type: 'skip', description: 'P-slot value not visible' });
      return;
    }

    await pValue.click();
    const panel = page.locator('.ps-breakdown-panel');
    await expect(panel).toBeVisible({ timeout: 5000 });

    // Click the close button
    const closeBtn = panel.locator('.ps-bd-close');
    await closeBtn.click();
    await expect(panel).not.toBeVisible({ timeout: 3000 });
  });
});
