/**
 * Verifies the Chases section visibility gate on /orders.
 *
 * 2026-10-23 — migrated off the dual-mount `{#if}/{:else}` swap (commit
 * a7f60400's gate) onto the single-instance `headless` pattern already
 * used by (algo)/+layout.svelte's header drawer. The `<section
 * class="bucket-card-chase">` wrapper and its one `<ChaseCard>` child are
 * now BOTH always mounted; visibility is driven by an inline
 * `style="display: none"` on the section (`style:display={_showChases ?
 * null : 'none'}`) plus `headless={!_showChases}` on ChaseCard itself —
 * never a template-level `{#if}`/`{:else}` around either element. There
 * is no separate hidden poller `<div>` anymore; ChaseCard's own polling
 * continues in the background regardless of visibility.
 *
 * Invariants:
 *   1. Status strip (.oc-filter-card buttons) shows 5 chips including Open.
 *   2. Exactly one `section.bucket-card-chase` exists, always (no XOR with
 *      a separate hidden-poller container — that container no longer
 *      exists at all).
 *   3. Visibility (CSS, not presence/absence) is consistent with Open
 *      order count.
 *   4. The section's inline `display: none` is present exactly when it
 *      should be hidden, and absent exactly when visible.
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

  test('exactly one chase section container exists, always mounted', async ({ page }) => {
    const chaseSection = await page.locator('section.bucket-card-chase').count();
    const hiddenPoller = await page.locator('div[style*="display:none"]').count();

    // The section is no longer template-gated — it's always in the DOM
    // now, visible or not. There is no separate hidden-poller <div> to
    // XOR against anymore (that pattern is gone entirely).
    expect(chaseSection, 'section.bucket-card-chase should always be mounted').toBe(1);
    expect(hiddenPoller, 'the old hidden-poller div must no longer exist').toBe(0);
  });

  test('gate state (visibility) is consistent with Open order count', async ({ page }) => {
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

    // The section is always present now — "visible" means CSS-visible
    // (display != none), not presence/absence in the DOM.
    const chaseVisible = await page.locator('section.bucket-card-chase').isVisible();

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

  test('section carries an inline display:none exactly when hidden, and no hidden-poller div exists either way', async ({ page }) => {
    const chaseVisible = await page.locator('section.bucket-card-chase').isVisible();
    const sectionHasDisplayNone = (await page.locator('section.bucket-card-chase')
      .getAttribute('style') ?? '').includes('display: none') ||
      (await page.locator('section.bucket-card-chase').getAttribute('style') ?? '').includes('display:none');
    const hiddenDivCount = await page.locator('div[style*="display:none"]').count();

    // There is no separate hidden-poller <div> in either state anymore —
    // the single ChaseCard instance stays mounted inside the (always
    // present) section regardless of visibility.
    expect(hiddenDivCount).toBe(0);

    if (!chaseVisible) {
      expect(sectionHasDisplayNone, 'hidden section should carry inline display:none').toBeTruthy();
    } else {
      expect(sectionHasDisplayNone, 'visible section must not carry display:none').toBeFalsy();
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
      // The section is always mounted now (visible or hidden via inline
      // style) — no fallback to a separate hidden-poller div is needed.
      const chase = document.querySelector('section.bucket-card-chase');
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
