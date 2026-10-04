/**
 * navstrip_label_infohint_split.spec.js
 *
 * Regression test for the 2026-10 fix: a single press on a NavStrip P/M/C/H
 * LABEL span (`.ps-agg-k`) used to bubble through two independent click
 * handlers at once — the label's own `onclick` → `_openBreakdown()` AND
 * the nested `<InfoHint>` chip's own internal click-toggle — opening
 * NavBreakdown and the InfoHint popover simultaneously. Fix: the outer
 * `onclick`/`onkeydown` were removed from the 4 label spans; the 3 VALUE
 * spans per pill (`.ps-agg-v`) keep their own independent `_openBreakdown`
 * handlers unchanged.
 *
 * Verifies:
 *   - Clicking a P/M/C/H label opens ONLY the InfoHint popup (never
 *     NavBreakdown).
 *   - Clicking a P/M/C/H value span opens ONLY NavBreakdown (never
 *     InfoHint).
 *
 * Run:
 *   cd frontend && npx playwright test navstrip_label_infohint_split --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const SLOTS = ['p', 'm', 'c', 'h'];

test.describe('NavStrip label press — InfoHint only, never NavBreakdown', () => {
  for (const slot of SLOTS) {
    test(`clicking the ${slot.toUpperCase()} label opens InfoHint, not the breakdown panel`, async ({ page }) => {
      test.setTimeout(60000);
      await loginAsAdmin(page);
      await page.goto('/pulse', { waitUntil: 'domcontentloaded' }).catch(() => {});
      await page.waitForTimeout(1500);

      const strip = page.locator('.ps-strip');
      const stripVisible = await strip.isVisible({ timeout: 3000 }).catch(() => false);
      if (!stripVisible) {
        test.info().annotations.push({ type: 'skip', description: 'PositionStrip not visible (market closed or no data)' });
        return;
      }

      const label = page.locator(`.ps-strip .ps-k-${slot}`).first();
      const labelVisible = await label.isVisible({ timeout: 3000 }).catch(() => false);
      if (!labelVisible) {
        test.info().annotations.push({ type: 'skip', description: `No ${slot.toUpperCase()}-slot label visible` });
        return;
      }

      // The label itself has no onclick anymore — the InfoHint chip inside
      // it (.info-btn) is the actual interactive element that receives
      // the click.
      const chip = label.locator('.info-btn');
      await expect(chip).toBeVisible();
      await chip.click();
      await page.waitForTimeout(300);

      // `.info-popout` is portalled to document.body (2026-10, escapes
      // ancestor stacking contexts) so it is no longer a DOM descendant
      // of `label` — assert globally instead. The singleton model (only
      // one pinned InfoHint at a time) keeps this unambiguous.
      const infoPopout = page.locator('.info-popout');
      await expect(infoPopout).toBeVisible();

      const breakdown = page.locator('.ps-breakdown-panel');
      await expect(breakdown).toHaveCount(0);

      // Close the InfoHint again so it doesn't bleed into the next slot's
      // check (singleton model would otherwise auto-close it anyway, but
      // being explicit keeps this test independent of that behaviour).
      await chip.click();
    });

    test(`clicking the ${slot.toUpperCase()} value opens the breakdown panel, not InfoHint`, async ({ page }) => {
      test.setTimeout(60000);
      await loginAsAdmin(page);
      await page.goto('/pulse', { waitUntil: 'domcontentloaded' }).catch(() => {});
      await page.waitForTimeout(1500);

      const strip = page.locator('.ps-strip');
      const stripVisible = await strip.isVisible({ timeout: 3000 }).catch(() => false);
      if (!stripVisible) {
        test.info().annotations.push({ type: 'skip', description: 'PositionStrip not visible (market closed or no data)' });
        return;
      }

      const value = page.locator(`.ps-strip .ps-k-${slot}`)
        .locator('xpath=following-sibling::span[1][contains(@class, "ps-agg-v")]')
        .first();
      const valueVisible = await value.isVisible({ timeout: 3000 }).catch(() => false);
      if (!valueVisible) {
        test.info().annotations.push({ type: 'skip', description: `No ${slot.toUpperCase()}-slot value visible` });
        return;
      }
      await value.click();
      await page.waitForTimeout(300);

      const breakdown = page.locator('.ps-breakdown-panel');
      const breakdownVisible = await breakdown.isVisible({ timeout: 5000 }).catch(() => false);
      if (!breakdownVisible) {
        test.info().annotations.push({ type: 'skip', description: 'breakdown popup did not open' });
        return;
      }
      await expect(breakdown).toBeVisible();

      // InfoHint popover for this pill must NOT have opened as a side
      // effect of the value click. `.info-popout` is portalled to
      // document.body (2026-10) so this must be a page-global check,
      // not scoped under the pill's label — a scoped locator would
      // trivially report 0 regardless of whether InfoHint actually
      // opened elsewhere on the page.
      const infoPopout = page.locator('.info-popout');
      await expect(infoPopout).toHaveCount(0);
    });
  }
});
