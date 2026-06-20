/**
 * Verifies the Chases section visibility gate on /orders (commit a7f60400).
 *
 * Gate logic:
 *   {#if _openOrderCount > 0 || _activeChases > 0}
 *     <section class="bucket-card bucket-card-chase">...</section>
 *   {:else}
 *     <div style="display:none"><ChaseCard … /></div>  ← hidden poller
 *   {/if}
 *
 * Invariants:
 *   1. Status strip (.oc-filter-card buttons) shows 5 chips including Open.
 *   2. Exactly ONE of: visible chase section OR hidden-div poller exists.
 *   3. Gate state is consistent with Open order count.
 *   4. When hidden, display:none poller div is mounted.
 *   5. LogPanel Activity section is present.
 *   6. LogPanel default-active tab is "Order".
 *   7. Document order: chase region appears before Activity section.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE_URL = 'https://ramboq.com';
const TIMEOUT = 30_000;

test.describe('/orders — Chases section visibility gate', () => {
  test.use({ baseURL: BASE_URL });

  // Increase to 90 s to absorb the up-to-8 s rate-limit retry inside loginAsAdmin
  // plus prod page load time plus the 5 s chase-poller settle wait.
  test.setTimeout(90_000);

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    // Wait for the status strip's filter cards to appear — these are
    // always rendered regardless of order count.
    await page.waitForSelector('.oc-filter-card', { timeout: TIMEOUT });
    // Give the hidden ChaseCard poller 5 s to fire its first poll and
    // update _activeChases so we read the settled gate state.
    await page.waitForTimeout(5000);
  });

  test('status strip has 5 filter cards including Open', async ({ page }) => {
    const cards = page.locator('.oc-filter-card');
    await expect(cards).toHaveCount(5);

    // Confirm the "Open" label chip is one of them.
    const labels = await cards.evaluateAll(els =>
      els.map(el => el.querySelector('.oc-filter-label')?.textContent?.trim() ?? '')
    );
    expect(labels).toContain('Open');
  });

  test('exactly one chase-related container exists (section XOR hidden div)', async ({ page }) => {
    const chaseSection = await page.locator('section.bucket-card-chase').count();
    const hiddenPoller = await page.locator('div[style*="display:none"]').count();

    // The gate is {#if} / {:else} — exactly one branch is rendered.
    expect(
      chaseSection + hiddenPoller,
      'Exactly one chase container should exist',
    ).toBe(1);
  });

  test('gate state is consistent with Open order count', async ({ page }) => {
    // Read the "Open" count from the .oc-filter-count inside the Open card.
    const openCount = await page.evaluate(() => {
      const cards = Array.from(document.querySelectorAll('.oc-filter-card'));
      for (const card of cards) {
        const label = card.querySelector('.oc-filter-label')?.textContent?.trim();
        if (label === 'Open') {
          const count = card.querySelector('.oc-filter-count')?.textContent?.trim();
          return count ? parseInt(count, 10) : 0;
        }
      }
      return 0;
    });

    const chaseVisible = await page.locator('section.bucket-card-chase').count() > 0;

    console.log(`Open count: ${openCount}, Chase section visible: ${chaseVisible}`);

    if (openCount > 0) {
      // Open orders must mean the section is visible.
      expect(chaseVisible, 'Chases section must be visible when Open > 0').toBeTruthy();
    } else {
      // Open=0: section may be hidden (idle book) or still visible due
      // to _activeChases > 0 (background paper-engine chases). Both valid.
      // The only invalid state: section hidden but its visible inner content
      // is somehow rendering outside the section.
      if (chaseVisible) {
        const innerText = await page.locator('section.bucket-card-chase').innerText();
        expect(
          innerText.trim().length,
          'Visible chase section must have non-empty content',
        ).toBeGreaterThan(0);
      }
    }
  });

  test('when hidden, display:none poller div is present; when visible, it is absent', async ({ page }) => {
    const chaseVisible = await page.locator('section.bucket-card-chase').count() > 0;
    const hiddenDivCount = await page.locator('div[style*="display:none"]').count();

    if (!chaseVisible) {
      // Gate rendered the {:else} branch — exactly one hidden div must be there.
      expect(hiddenDivCount).toBe(1);
    } else {
      // Gate rendered {#if} branch — no display:none div should exist.
      expect(hiddenDivCount).toBe(0);
    }
  });

  test('LogPanel Activity section is present in the page', async ({ page }) => {
    const activitySection = page.locator('section.bucket-card-activity').first();
    await expect(activitySection).toBeAttached({ timeout: TIMEOUT });
    await expect(activitySection).toBeVisible({ timeout: TIMEOUT });
  });

  test('LogPanel default-active tab is Order', async ({ page }) => {
    // AlgoTabs renders .algo-tab buttons with aria-selected="true" on the active one.
    // Wait for the tab strip to mount inside the Activity card.
    const activeTab = page.locator('section.bucket-card-activity .algo-tab[aria-selected="true"]').first();
    await expect(activeTab).toBeVisible({ timeout: TIMEOUT });
    const tabText = await activeTab.textContent();
    expect(tabText?.trim().toLowerCase()).toContain('order');
  });

  test('document order: chase region appears before Activity/LogPanel section', async ({ page }) => {
    const result = await page.evaluate(() => {
      const chase =
        document.querySelector('section.bucket-card-chase') ||
        document.querySelector('div[style*="display:none"]');
      const activity = document.querySelector('section.bucket-card-activity');
      if (!chase || !activity) return null;

      // DOCUMENT_POSITION_FOLLOWING (4) is set on the result when
      // `activity` follows `chase` in document order.
      const pos = chase.compareDocumentPosition(activity);
      return {
        pos,
        isFollowing: !!(pos & Node.DOCUMENT_POSITION_FOLLOWING),
      };
    });

    expect(result, 'Both chase region and Activity section must be found').not.toBeNull();
    expect(
      result.isFollowing,
      `Activity must follow chase region in DOM (compareDocumentPosition=${result.pos})`,
    ).toBeTruthy();
  });
});
