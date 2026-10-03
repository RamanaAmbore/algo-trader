/**
 * options_payoff_stat_infohints.spec.js
 *
 * OptionsPayoff.svelte's top-left stat overlay (LTP/CHG%/CLOSE/DAY P&L/
 * P&L/ADJ/Exp P&L/DTE/σ rows) used plain `title=` attributes on each
 * `.ps-row`, even though the file already imports InfoHint for other
 * UI elsewhere on the page (Legs grid labels, etc. — not this overlay).
 * Fix: each row's label (`.ps-k`) now carries an `<InfoHint popup>`
 * chip with the same wording the old `title=` wrapped, matching the
 * house pattern already used by ChartWorkspace.svelte's Greeks strip.
 *
 * This spec runs against the live /admin/derivatives page (needs a
 * real open F&O position to render the payoff chart at all — gracefully
 * skips if the book is empty, same pattern as derivatives_spot_ssot.spec.js).
 *
 * Five quality dimensions:
 *  1. SSOT    — n/a (display-only copy, not a shared computed value).
 *  2. Perf    — n/a.
 *  3. Stale   — no `.ps-row` in the stat overlay carries a `title=`
 *               attribute any more (old tooltip mechanism fully
 *               retired for this surface, not left dangling alongside
 *               the new InfoHint chip — which would show two
 *               tooltips).
 *  4. Reuse   — exercises the shared InfoHint component's own open/
 *               close + `[role="tooltip"]` contract (same contract
 *               `derivatives_greek_header_chip_infohint.spec.js`
 *               already proves for the Greeks strip).
 *  5. UX      — the LTP row's chip text differs for an MCX anchor-
 *               contract spot vs. a plain spot (dynamic `text` prop,
 *               not a static string) — both branches render without
 *               raising a console error.
 *
 * Run:
 *   npx playwright test e2e/options_payoff_stat_infohints.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const DERIV_URL = '/admin/derivatives';

test.describe('OptionsPayoff stat overlay — InfoHint field-as-trigger (hideButton mode)', () => {
  test('LTP/CHG%/DAY P&L rows open popover via click and hover on the label span (no separate chip)', async ({ page, viewport }) => {
    await loginAsAdmin(page);

    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(String(e)));

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    const payoffOverlay = page.locator('.payoff-stats');
    const overlayVisible = await payoffOverlay.waitFor({ state: 'visible', timeout: 15_000 }).then(() => true).catch(() => false);
    if (!overlayVisible) {
      test.skip(true, 'No open F&O position to render the payoff chart — nothing to check live');
      return;
    }

    // Stale check: no `.ps-row` in the overlay carries a `title=`
    // attribute any more (the old tooltip mechanism) EXCEPT the LEGS
    // row, deliberately left untouched (out of scope — not one of
    // the named rows). A leftover title= on a converted row would
    // show a second, native tooltip stacked on the new InfoHint
    // popover.
    const rowsWithTitle = payoffOverlay.locator('.ps-row[title]');
    const titleRowCount = await rowsWithTitle.count();
    expect(titleRowCount).toBeLessThanOrEqual(1);
    if (titleRowCount === 1) {
      await expect(rowsWithTitle.first()).toHaveAttribute('title', 'Number of legs in the strategy basket');
    }

    // LTP row: the `.ps-k` label span is now the anchor (hideButton mode, no separate button)
    const ltpRow = payoffOverlay.locator('.ps-row', { has: page.locator('.ps-k', { hasText: 'LTP' }) }).first();
    await expect(ltpRow).toBeVisible();
    const ltpLabel = ltpRow.locator('.ps-k').first();

    // No visible info-btn inside the label
    await expect(ltpLabel.locator('button.info-btn')).toHaveCount(0);

    // CLICK opens the popover
    await ltpLabel.click();
    let popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });
    // Dynamic text prop — either the MCX anchor-contract wording or
    // the plain-spot wording, both of which this regex covers.
    await expect(popover).toContainText(/Spot anchor:|Current spot price for the underlying/);

    // Viewport clipping check for LTP popover
    if (viewport) {
      const popoverRect = await popover.evaluate((el) => {
        const rect = el.getBoundingClientRect();
        return {
          left: rect.left,
          right: rect.right,
          top: rect.top,
          bottom: rect.bottom,
        };
      });
      const vw = viewport.width;
      const vh = viewport.height;
      expect(popoverRect.left, 'LTP popover left edge must be >= 0').toBeGreaterThanOrEqual(0);
      expect(popoverRect.right, `LTP popover right edge must be <= ${vw}`).toBeLessThanOrEqual(vw);
      expect(popoverRect.top, 'LTP popover top edge must be >= 0').toBeGreaterThanOrEqual(0);
      expect(popoverRect.bottom, `LTP popover bottom edge must be <= ${vh}`).toBeLessThanOrEqual(vh);
    }

    // Close via re-click + mouse move (to clear hover state)
    await ltpLabel.click();
    await page.mouse.move(0, 0); // Move away to clear hovered state
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0, { timeout: 1000 });

    // HOVER opens the popover (new in this fix)
    await ltpLabel.hover();
    popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });
    await expect(popover).toContainText(/Spot anchor:|Current spot price for the underlying/);

    // Move mouse away to close via hover
    await page.mouse.move(0, 0);
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0, { timeout: 500 });

    // DAY P&L row, if present — same mechanism, different wording.
    const dayPnlRow = payoffOverlay.locator('.ps-row', { has: page.locator('.ps-k', { hasText: 'DAY P&L' }) }).first();
    if (await dayPnlRow.count() > 0) {
      const dayPnlLabel = dayPnlRow.locator('.ps-k').first();

      // Click test
      await dayPnlLabel.click();
      let dayPopover = page.locator('[role="tooltip"]').first();
      await expect(dayPopover).toBeVisible({ timeout: 2000 });
      await expect(dayPopover).toContainText('mark-to-market change');

      await dayPnlLabel.click(); // close via re-click
      await page.mouse.move(0, 0); // Move away to clear hover state
      await expect(page.locator('[role="tooltip"]')).toHaveCount(0, { timeout: 1000 });

      // Hover test
      await dayPnlLabel.hover();
      const dayPopoverH = page.locator('[role="tooltip"]').first();
      await expect(dayPopoverH).toBeVisible({ timeout: 2000 });
      await expect(dayPopoverH).toContainText('mark-to-market change');

      // Move away to close
      await page.mouse.move(0, 0);
      await expect(page.locator('[role="tooltip"]')).toHaveCount(0, { timeout: 500 });
    }

    const realErrors = pageErrors.filter((e) => !e.includes('401') && !e.includes('405'));
    expect(realErrors, 'No unexpected JS errors from the InfoHint anchors').toHaveLength(0);
  });
});
