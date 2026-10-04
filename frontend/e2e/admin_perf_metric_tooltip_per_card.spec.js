/**
 * admin_perf_metric_tooltip_per_card.spec.js
 *
 * Perf page headline stats (LOC, cc max, cc avg, LCP on FE cards;
 * LOC, cc max, cc avg, p95 on BE cards) converted to field-as-trigger
 * `hideButton` mode with per-card dynamic keying.
 *
 * The key concern: stats repeat per card in {#each feCards} / {#each beCards}
 * loops. State + anchor dicts are keyed dynamically by
 * `_perfHintKey('fe', card, 'loc')` etc. — ensuring every card instance
 * has independent popover state, not one shared across all cards.
 *
 * This spec verifies the keying actually works: opening card A's LOC tooltip
 * does NOT also open card B's LOC tooltip (would happen with a shared key or
 * missing keying logic).
 *
 * Run:
 *   npx playwright test e2e/admin_perf_metric_tooltip_per_card.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const PERF_URL = '/admin/perf';

test.describe('Admin Perf page — per-card metric tooltip independence', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(PERF_URL, { waitUntil: 'domcontentloaded', timeout: 30_000 });
  });

  test('Per-card LOC tooltips are independent (card A LOC open ≠ card B LOC open)', async ({ page, viewport }) => {
    // Wait for at least two FE cards to render
    const feSection = page.locator('text=Frontend').first();
    await expect(feSection).toBeVisible({ timeout: 15_000 });

    const feCards = page.locator('.perf-card'); // Container for each FE card
    const cardCount = await feCards.count();
    if (cardCount < 2) {
      test.skip(true, 'Fewer than 2 FE cards rendered — cannot test per-card independence');
      return;
    }

    // Find LOC labels in the first two cards
    const firstCard = feCards.nth(0);
    const secondCard = feCards.nth(1);

    const firstLocLabel = firstCard.locator('.metric-label:has-text("LOC")').first();
    const secondLocLabel = secondCard.locator('.metric-label:has-text("LOC")').first();

    await expect(firstLocLabel).toBeVisible();
    await expect(secondLocLabel).toBeVisible();

    // OPEN card A's LOC tooltip
    await firstLocLabel.click();
    let popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });

    // Verify it's actually open (contains tooltip content)
    const locHint = await popover.locator('.info-dt:has-text("WHAT")').count();
    expect(locHint).toBeGreaterThan(0);

    // Get the popover's position to confirm it's anchored to card A, not card B
    const popoverRect = await popover.evaluate((el) => {
      const rect = el.getBoundingClientRect();
      return {
        left: rect.left,
        top: rect.top,
        centerX: rect.left + rect.width / 2,
      };
    });

    // Verify the popover is near card A (not floated far away or near card B)
    const firstCardRect = await firstCard.evaluate((el) => {
      const rect = el.getBoundingClientRect();
      return {
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom,
      };
    });

    // Popover should be reasonably close to card A (within viewport)
    // Allow for overflow positioning, so just check it didn't jump to card B
    const secondCardRect = await secondCard.evaluate((el) => {
      const rect = el.getBoundingClientRect();
      return {
        left: rect.left,
        right: rect.right,
        top: rect.top,
        centerX: rect.left + rect.width / 2,
      };
    });

    // The key check: popover's horizontal center is closer to card A than card B
    const distToA = Math.abs(popoverRect.centerX - firstCardRect.left);
    const distToB = Math.abs(popoverRect.centerX - secondCardRect.centerX);
    expect(distToA).toBeLessThan(distToB);

    // Close card A's tooltip
    await firstLocLabel.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

    // NOW open card B's LOC tooltip
    await secondLocLabel.click();
    popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });

    // Verify card B's popover position is different from card A's would have been
    const popoverBRect = await popover.evaluate((el) => {
      const rect = el.getBoundingClientRect();
      return {
        left: rect.left,
        top: rect.top,
        centerX: rect.left + rect.width / 2,
      };
    });

    // Card B's popover should be closer to card B than card A
    const distToBFromB = Math.abs(popoverBRect.centerX - secondCardRect.centerX);
    const distToAFromB = Math.abs(popoverBRect.centerX - firstCardRect.left);
    expect(distToBFromB).toBeLessThan(distToAFromB);
  });

  test('Different metrics on same card are independent (LOC ≠ cc max)', async ({ page }) => {
    // Verify that opening one metric's tooltip and then another on the same
    // card closes the first and shows only the second — per-metric independence.
    const feSection = page.locator('text=Frontend').first();
    await expect(feSection).toBeVisible({ timeout: 15_000 });

    const firstCard = page.locator('.perf-card').nth(0);
    const locLabel = firstCard.locator('.metric-label:has-text("LOC")').first();
    const ccMaxLabel = firstCard.locator('.metric-label:has-text("cc max")').first();

    await expect(locLabel).toBeVisible();
    await expect(ccMaxLabel).toBeVisible();

    // Open LOC
    await locLabel.click();
    let popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });
    let text = (await popover.textContent()) || '';
    expect(text).toContain('LOC') || expect(text).toContain('lines');

    // Open cc max — should close LOC
    await ccMaxLabel.click();
    const tooltips = page.locator('[role="tooltip"]');
    await expect(tooltips).toHaveCount(1);
    text = (await tooltips.first().textContent()) || '';
    expect(text).toContain('cc') || expect(text).toContain('complexity');
  });

  test('Per-card tooltips show a hover PREVIEW (not a pin) and pin only on click (per-card state isolated)', async ({ page }) => {
    // 2026-10 hover-preview + click-to-pin reintroduction, modeled on
    // OptionsPayoff.svelte's own hover/pin tooltip: hovering a label
    // shows a transient preview that never pins and never claims the
    // app-wide singleton; only a click pins it.
    const feSection = page.locator('text=Frontend').first();
    await expect(feSection).toBeVisible({ timeout: 15_000 });

    const feCards = page.locator('.perf-card');
    const cardCount = await feCards.count();
    if (cardCount < 1) {
      test.skip(true, 'No FE cards rendered');
      return;
    }

    const firstCard = feCards.nth(0);
    const locLabel = firstCard.locator('.metric-label:has-text("LOC")').first();
    await expect(locLabel).toBeVisible();

    // HOVER shows a preview. A real `.hover()` proves the cursor
    // actually landed on the label.
    await locLabel.hover();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await expect(locLabel).toHaveAttribute('aria-expanded', 'false'); // preview only — not pinned

    // Moving away drops the preview — it never pinned.
    await page.mouse.move(5, 5);
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

    // CLICK to pin open
    await locLabel.click();
    const popoverClick = page.locator('[role="tooltip"]').first();
    await expect(popoverClick).toBeVisible({ timeout: 2000 });
    await expect(popoverClick).toHaveAttribute('role', 'tooltip');
    await expect(locLabel).toHaveAttribute('aria-expanded', 'true');

    // Click to unpin
    await locLabel.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
  });

  test('Hotspot cc table header tooltip is also independent (single site, not looped)', async ({ page }) => {
    // The hotspot table header "cc" label is a single fixed site (not inside
    // an {#each} loop). Verify it also uses the hideButton+anchor pattern and works.
    const hotspotSection = page.locator('text=hotspot').first();
    if (await hotspotSection.count() === 0) {
      test.skip(true, 'Hotspot section not visible');
      return;
    }

    const ccHeader = page.locator('.metrics-table th.algo-table-num', { hasText: 'cc' }).first();
    const ccLabel = ccHeader.locator('.metric-label').first();

    await expect(ccLabel).toBeVisible();
    await expect(ccLabel).toHaveAttribute('role', 'button');
    await expect(ccLabel.locator('button.info-btn')).toHaveCount(0);

    // CLICK test
    await ccLabel.click();
    const popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });
    await expect(popover).toContainText(/cc|complexity/);

    // Close
    await ccLabel.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
  });
});
