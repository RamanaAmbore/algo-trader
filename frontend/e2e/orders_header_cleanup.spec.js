/**
 * orders_header_cleanup.spec.js
 *
 * 2026-10-13 "Header button-group standardization" — Part 1 (call-site
 * cleanup) + Part 6 (Order Entry label structure), scoped to
 * `(algo)/orders/+page.svelte` only (companion agents handle the
 * shared `CardControls.svelte`/`CollapseButton.svelte` removal and the
 * other 20 call sites in parallel — not re-tested here).
 *
 * Part 1 — the Order Entry and Order Activity bucket-cards on /orders
 *   used to bind `isCollapsed`/`class:is-collapsed` for a chevron
 *   Collapse button that CardControls.svelte no longer renders at all.
 *   This spec proves no Collapse/caret button exists anywhere on the
 *   page (not just hidden) and that the surviving Fullscreen button
 *   on each card still works post-cleanup (regression guard — removing
 *   one button in a shared group must not break its siblings).
 *
 * Part 6 (SUPERSEDED 2026-10, operator request) — the "Order Entry"
 *   card's whole header row (the `.oc-entry-label` title + icon + its
 *   card-control trio) was removed entirely, not just restyled. This
 *   spec now confirms no header/label renders at all, replacing the
 *   earlier border-left/color structure checks.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/orders_header_cleanup.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

async function gotoOrders(page) {
  await loginAsAdmin(page);
  await page.route('**/api/orders/held/**', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ held: [] }),
  }));
  await page.route('**/api/orders/chases/active**', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify([]),
  }));
  await page.route('**/api/orders/**', (route) => {
    const url = route.request().url();
    // Let the two routes above (held/, chases/active) win first —
    // Playwright matches most-recently-registered route first, so
    // this catch-all only ever serves the plain orders list fetch.
    if (url.includes('/orders/held/') || url.includes('/orders/chases/active')) {
      return route.fallback();
    }
    if (route.request().method() === 'GET') {
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: [] }),
      });
    }
    return route.fallback();
  });
  await page.goto('/orders', { waitUntil: 'domcontentloaded' });
}

test.describe('Orders page — Part 1: Collapse removal, no dead wiring', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test('no Collapse/caret button renders on Order Entry or Order Activity cards', async ({ page }) => {
    await gotoOrders(page);

    const entryCard = page.locator('.bucket-card-entry');
    const activityCard = page.locator('.bucket-card-activity');
    await expect(entryCard).toBeVisible({ timeout: 20_000 });
    await expect(activityCard).toBeVisible({ timeout: 20_000 });

    // No collapse-btn element, no caret SVG with the Collapse a11y
    // label, anywhere on the page — not scoped to one card, since a
    // leftover render anywhere would prove the removal was incomplete.
    await expect(page.locator('.collapse-btn')).toHaveCount(0);
    await expect(page.locator('[aria-label*="ollapse" i]')).toHaveCount(0);
    await expect(page.locator('[title*="ollapse" i]')).toHaveCount(0);

    // Neither card ever carries the dead `is-collapsed` class — proves
    // the wiring itself was removed, not just the button visually
    // hidden behind unreachable state.
    await expect(entryCard).not.toHaveClass(/is-collapsed/);
    await expect(activityCard).not.toHaveClass(/is-collapsed/);
  });

  test('Order Entry card has no header row at all (removed entirely, 2026-10 operator request)', async ({ page }) => {
    await gotoOrders(page);

    const entryCard = page.locator('.bucket-card-entry');
    await expect(entryCard).toBeVisible({ timeout: 20_000 });

    // The whole header row — title, Fullscreen, Refresh — is gone, not
    // just restyled. No `.card-header` at all inside this card anymore,
    // so there is also no Fullscreen/Refresh control left to regression-
    // guard here (removing them was the explicit ask).
    await expect(entryCard.locator('.card-header')).toHaveCount(0);
    await expect(entryCard.locator('.fs-btn, .default-btn')).toHaveCount(0);
  });

  test('Fullscreen button survives on Order Activity (OrderBook) card after Collapse removal (regression guard)', async ({ page }) => {
    await gotoOrders(page);

    const activityCard = page.locator('.bucket-card-activity');
    await expect(activityCard).toBeVisible({ timeout: 20_000 });

    const expandBtn = activityCard.locator('.fs-btn').first();
    await expect(expandBtn).toBeVisible();
    await expandBtn.click();
    await expect(activityCard).toHaveClass(/fs-card-on/);

    const restoreBtn = activityCard.locator('.default-btn').first();
    await expect(restoreBtn).toBeVisible();
    await restoreBtn.click();
    await expect(activityCard).not.toHaveClass(/fs-card-on/);
  });
});

test.describe('Orders page — Part 6 (superseded 2026-10): Order Entry label removed along with the whole header', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test('no "ORDER ENTRY" label (.oc-entry-label) renders anywhere on the page', async ({ page }) => {
    await gotoOrders(page);

    const entryCard = page.locator('.bucket-card-entry');
    await expect(entryCard).toBeVisible({ timeout: 20_000 });

    // The label this spec used to style-check no longer exists at all —
    // the whole header row (title + icon + card-controls) was removed,
    // not just restyled (2026-10, operator request).
    await expect(page.locator('.oc-entry-label')).toHaveCount(0);
    await expect(page.locator('text=ORDER ENTRY')).toHaveCount(0);
  });
});
