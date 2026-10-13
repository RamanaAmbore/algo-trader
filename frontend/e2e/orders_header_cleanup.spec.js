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
 * Part 6 — the "Order Entry" card's title is a CUSTOM `left`-snippet
 *   span (`.oc-entry-label`), not CardHeader's plain `title` prop, so
 *   the CardHeader-sweep round 3 Part A `.ch-title` border-left fix
 *   never reached it. This spec confirms `.oc-entry-label` now has
 *   the same `border-left: 3px solid currentColor` + left-padding
 *   structure, while its own deliberate amber color (writing-surface /
 *   operator-action semantic, see the page's own "Card chrome" comment)
 *   is UNCHANGED — a regression guard that the color stays amber, not
 *   just "a border was added".
 *
 * Run:
 *   cd frontend && npx playwright test e2e/orders_header_cleanup.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

/** Parses a computed `rgb(...)`/`rgba(...)` color string into
 *  {r,g,b,a} (a defaults to 1 for 3-channel `rgb(...)`). */
function parseRGBA(str) {
  const m = /rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)/.exec(str || '');
  if (!m) return null;
  return { r: Number(m[1]), g: Number(m[2]), b: Number(m[3]), a: m[4] !== undefined ? Number(m[4]) : 1 };
}

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

  test('Fullscreen button survives on Order Entry card after Collapse removal (regression guard)', async ({ page }) => {
    await gotoOrders(page);

    const entryCard = page.locator('.bucket-card-entry');
    await expect(entryCard).toBeVisible({ timeout: 20_000 });

    // FullscreenButton (.fs-btn, "expand") and DefaultSizeButton
    // (.default-btn, "restore") are an either/or pair — only one is
    // mounted at a time depending on isFullscreen's current value.
    const expandBtn = entryCard.locator('.fs-btn').first();
    await expect(expandBtn).toBeVisible();
    await expandBtn.click();
    await expect(entryCard).toHaveClass(/fs-card-on/);

    const restoreBtn = entryCard.locator('.default-btn').first();
    await expect(restoreBtn).toBeVisible();
    await restoreBtn.click();
    await expect(entryCard).not.toHaveClass(/fs-card-on/);
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

test.describe('Orders page — Part 6: Order Entry label border-left + padding, amber color unchanged', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test('.oc-entry-label has border-left + left padding, text stays amber', async ({ page }) => {
    await gotoOrders(page);

    const label = page.locator('.oc-entry-label', { hasText: 'ORDER ENTRY' }).first();
    await expect(label).toBeVisible({ timeout: 20_000 });

    const info = await label.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        color: s.color,
        borderLeftColor: s.borderLeftColor,
        borderLeftWidth: s.borderLeftWidth,
        borderLeftStyle: s.borderLeftStyle,
        paddingLeft: s.paddingLeft,
      };
    });

    // Structure now matches .ch-title / .mp-bucket-label.
    expect(info.borderLeftWidth, 'border-left must actually render (non-zero width)').toBe('3px');
    expect(info.borderLeftStyle).toBe('solid');
    const paddingPx = parseFloat(info.paddingLeft);
    expect(paddingPx, `padding-left should be > 0, got "${info.paddingLeft}"`).toBeGreaterThan(0);

    // Color is UNCHANGED — still the deliberate amber-400 @ 70% alpha
    // (rgba(251, 191, 36, 0.7)), not the new neutral .ch-title default.
    const textRgba = parseRGBA(info.color);
    expect(textRgba, `label color should be rgb(), got "${info.color}"`).not.toBeNull();
    expect(textRgba.r).toBeCloseTo(251, 0);
    expect(textRgba.g).toBeCloseTo(191, 0);
    expect(textRgba.b).toBeCloseTo(36, 0);
    expect(textRgba.a).toBeCloseTo(0.7, 1);

    // border-left: 3px solid currentColor — border tracks the SAME
    // resolved color as the text (same invariant .ch-title's own test
    // checks), not a second independent color.
    const borderRgba = parseRGBA(info.borderLeftColor);
    expect(borderRgba, `border-left-color should be rgb(), got "${info.borderLeftColor}"`).not.toBeNull();
    expect(borderRgba.r).toBeCloseTo(textRgba.r, 0);
    expect(borderRgba.g).toBeCloseTo(textRgba.g, 0);
    expect(borderRgba.b).toBeCloseTo(textRgba.b, 0);
  });
});
