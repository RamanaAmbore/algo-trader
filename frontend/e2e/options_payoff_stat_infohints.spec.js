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
 * 2026-10 update (hover-preview + click-to-pin reintroduction): hovering
 * a `.ps-k` label now shows a transient preview again, modeled on
 * OptionsPayoff.svelte's own hover/pin tooltip for the chart's main
 * curve — but the preview never pins (`open` stays false) and never
 * claims the app-wide singleton. The assertions below verify the
 * preview appears/disappears with the cursor, and click remains the
 * only way to pin the popover open.
 *
 * Run:
 *   npx playwright test e2e/options_payoff_stat_infohints.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const DERIV_URL = '/admin/derivatives';

test.describe('OptionsPayoff stat overlay — InfoHint field-as-trigger (hideButton mode)', () => {
  test('LTP/CHG%/DAY P&L rows pin via CLICK on the label span (no separate chip); hover shows a preview only', async ({ page, viewport }) => {
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

    // Close via re-click
    await ltpLabel.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0, { timeout: 1000 });

    // HOVER shows a transient preview — wait out the 350ms post-dismiss
    // suppression window from the click-close above first. A real
    // `.hover()` proves the cursor actually landed on the label.
    await page.waitForTimeout(400);
    await ltpLabel.hover();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    await page.mouse.move(5, 5);
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

    // Positive control: click still pins it.
    await ltpLabel.click();
    popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });
    await expect(popover).toContainText(/Spot anchor:|Current spot price for the underlying/);
    await ltpLabel.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

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
      await expect(page.locator('[role="tooltip"]')).toHaveCount(0, { timeout: 1000 });

      // Hover shows a preview only, after the suppression window.
      // Dispatched directly (rather than a real `.hover()`) — this row
      // sits in the tightly-packed stat overlay where real cursor
      // travel between rows is unreliable; InfoHint's own pointerenter
      // handler is what's under test here, not cursor movement.
      await page.waitForTimeout(400);
      await dayPnlLabel.dispatchEvent('pointerenter', { pointerType: 'mouse' });
      await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
      await dayPnlLabel.dispatchEvent('pointerleave', { pointerType: 'mouse' });
      await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

      // Positive control: click still pins it.
      await dayPnlLabel.click();
      const dayPopoverC = page.locator('[role="tooltip"]').first();
      await expect(dayPopoverC).toBeVisible({ timeout: 2000 });
      await expect(dayPopoverC).toContainText('mark-to-market change');
      await dayPnlLabel.click();
      await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
    }

    const realErrors = pageErrors.filter((e) => !e.includes('401') && !e.includes('405'));
    expect(realErrors, 'No unexpected JS errors from the InfoHint anchors').toHaveLength(0);
  });

  // 2026-10: the popout is now portalled to document.body (see
  // `$lib/portal`) so it escapes `.payoff-stats`'s own stacking context
  // (position:absolute + z-index:3), which previously trapped the
  // popover underneath the chart's foreground SVG curve (a sibling of
  // `.payoff-stats`, explicitly z-index:4 so it redraws on top of the
  // stats overlay — see the file's own comment near `.payoff-svg-fg`).
  test('LTP popout escapes .payoff-stats via portal: not a DOM descendant, paints above the chart curve, matches hover-preview position, and own-content clicks do not close it', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    const payoffOverlay = page.locator('.payoff-stats');
    const overlayVisible = await payoffOverlay.waitFor({ state: 'visible', timeout: 15_000 }).then(() => true).catch(() => false);
    if (!overlayVisible) {
      test.skip(true, 'No open F&O position to render the payoff chart — nothing to check live');
      return;
    }

    const ltpRow = payoffOverlay.locator('.ps-row', { has: page.locator('.ps-k', { hasText: 'LTP' }) }).first();
    const ltpLabel = ltpRow.locator('.ps-k').first();

    // --- Pin via click, then verify the portal actually happened ---
    await ltpLabel.click();
    const popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });

    const isDescendantOfStats = await popover.evaluate((el) => !!el.closest('.payoff-stats'));
    expect(isDescendantOfStats, 'popout must escape .payoff-stats — it should no longer be a DOM descendant').toBe(false);

    const parentIsBody = await popover.evaluate((el) => el.parentElement === document.body);
    expect(parentIsBody, 'popout should be portalled directly onto document.body').toBe(true);

    // --- Paints above the chart's foreground curve SVG ---
    // The fg SVG (z-index 4, `.payoff-svg-fg`) normally sets
    // pointer-events:none so it never blocks chart hover/zoom — which
    // would make a plain document.elementFromPoint() probe pass
    // regardless of whether the portal fix is present (the SVG never
    // participates in hit-testing either way). To make this a genuine
    // regression check, inject a deterministic, fully-opaque-to-hit-
    // testing probe rect (fill="transparent" + pointer-events:auto,
    // not fill="none" — a "none" fill is NOT hit-testable) covering the
    // whole fg SVG, matching its real z-index tier. If the popout were
    // still trapped inside `.payoff-stats`'s local stacking context
    // (the bug), this probe — painted above that local context — would
    // win the hit test at the popout's own on-screen center. Once
    // portalled, the popout sits at --z-tooltip (20002) in the ROOT
    // stacking context, above everything, and wins instead.
    await page.evaluate(() => {
      const svg = document.querySelector('.payoff-svg-fg');
      if (!svg) return;
      const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      rect.setAttribute('x', '0');
      rect.setAttribute('y', '0');
      rect.setAttribute('width', '100%');
      rect.setAttribute('height', '100%');
      rect.setAttribute('fill', 'transparent');
      rect.setAttribute('data-test-probe', 'fg-hit-probe');
      rect.style.pointerEvents = 'auto';
      svg.appendChild(rect);
    });
    const popupWins = await popover.evaluate((el) => {
      const r = el.getBoundingClientRect();
      const x = r.left + r.width / 2;
      const y = r.top + r.height / 2;
      const hit = document.elementFromPoint(x, y);
      return hit === el || el.contains(hit);
    });
    await page.evaluate(() => {
      document.querySelector('[data-test-probe="fg-hit-probe"]')?.remove();
    });
    expect(popupWins, 'popup must win the hit-test above the chart fg SVG curve, not be painted over by it').toBe(true);

    // --- Clicking inside the popout's own content does not close it ---
    await popover.click({ position: { x: 4, y: 4 } });
    await expect(popover).toBeVisible();

    await ltpLabel.click(); // close via re-click
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0, { timeout: 1000 });

    // --- Hover-preview and click-pin render at the SAME position ---
    // Measured across two independent `fit()` runs (preview, then a
    // fresh pin after fully dismissing the preview) so the comparison
    // actually exercises the positioning logic twice, rather than
    // reading the same unchanged DOM rect back on an unrerun effect.
    // Dispatched directly (rather than a real `.hover()`) — the mouse
    // is already resting on `ltpLabel` from the close-click immediately
    // above, and a real `.hover()` on an element the cursor is already
    // over is not guaranteed to refire `pointerenter` (browsers only
    // fire enter events on a genuine state transition). Same rationale
    // and pattern already used for the DAY P&L row below.
    await page.waitForTimeout(400); // clear the 350ms post-dismiss hover suppression
    await ltpLabel.dispatchEvent('pointerenter', { pointerType: 'mouse' });
    const hoverPopover = page.locator('[role="tooltip"]').first();
    await expect(hoverPopover).toBeVisible({ timeout: 2000 });
    const hoverRect = await hoverPopover.evaluate((el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, top: r.top };
    });
    await ltpLabel.dispatchEvent('pointerleave', { pointerType: 'mouse' }); // dismiss the preview
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0, { timeout: 1000 });

    await ltpLabel.click(); // fresh click-pin — re-runs fit() from scratch
    const pinnedPopover = page.locator('[role="tooltip"]').first();
    await expect(pinnedPopover).toBeVisible({ timeout: 2000 });
    const pinnedRect = await pinnedPopover.evaluate((el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, top: r.top };
    });
    expect(Math.abs(hoverRect.left - pinnedRect.left), 'hover-preview and click-pinned popup must land at the same left').toBeLessThanOrEqual(3);
    expect(Math.abs(hoverRect.top - pinnedRect.top), 'hover-preview and click-pinned popup must land at the same top').toBeLessThanOrEqual(3);

    await ltpLabel.click(); // close via re-click
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0, { timeout: 1000 });
  });
});
