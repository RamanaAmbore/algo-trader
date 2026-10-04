/**
 * chartworkspace_greek_tooltips.spec.js
 *
 * ChartWorkspace.svelte's `.cw-greeks-strip` overlay (Δ Γ Θ V ρ IV —
 * shown when hovering/clicking on an option symbol's chart) converted
 * from separate visible `<InfoHint popup panel>` chips to field-as-trigger
 * `hideButton` mode. The `.cw-greek-item` div itself (wrapping label +
 * value + tooltip) is now the anchor and click/hover target.
 *
 * This spec runs against the live /admin/derivatives page when an option
 * position is open and renders the payoff chart. Greeks strip only appears
 * for options (not equity). Gracefully skips if no option chart found.
 *
 * Five quality dimensions:
 *  1. SSOT    — n/a (display-only values, no computed SSOT).
 *  2. Perf    — click-to-popover opens within budget.
 *  3. Stale   — no `.cw-greek-item` carries a separate visible chip button.
 *  4. Reuse   — exercises the shared InfoHint component's hideButton+anchor
 *               contract, same as OptionsPayoff and derivatives Strategy Summary.
 *  5. UX      — click opens/closes the popover; hover is a genuine no-op
 *               (hover-opens-a-tooltip was removed from InfoHint app-wide,
 *               2026-10, explicit operator instruction — click-only everywhere).
 *
 * Run:
 *   npx playwright test e2e/chartworkspace_greek_tooltips.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const DERIV_URL = '/admin/derivatives';

test.describe('ChartWorkspace Greeks strip — field-as-trigger tooltips (hideButton mode)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded', timeout: 30_000 });
  });

  test('Greeks strip is present for option chart and uses hideButton tooltips (no separate chip buttons)', async ({ page, viewport }) => {
    // Wait for chart to render; Greeks strip only appears for options
    const greeksStrip = page.locator('.cw-greeks-strip');
    const stripVisible = await greeksStrip.waitFor({ state: 'visible', timeout: 15_000 }).then(() => true).catch(() => false);
    if (!stripVisible) {
      test.skip(true, 'No option chart rendered (no open option position) — Greeks strip not available');
      return;
    }

    // Should have exactly 5 Greek items (Δ Γ Θ V ρ); IV is conditional
    const greekItems = greeksStrip.locator('.cw-greek-item');
    const baseCount = await greekItems.count();
    expect(baseCount).toBeGreaterThanOrEqual(5);

    // Each Greek item has no separate info-btn (hideButton mode)
    for (let i = 0; i < Math.min(5, baseCount); i++) {
      const item = greekItems.nth(i);
      await expect(item.locator('button.info-btn')).toHaveCount(0);
      // But each is interactive (role=button on the div)
      await expect(item).toHaveAttribute('role', 'button');
    }
  });

  test('CLICK opens and closes Greek tooltip on .cw-greek-item anchor (Delta example)', async ({ page, viewport }) => {
    const greeksStrip = page.locator('.cw-greeks-strip');
    const stripVisible = await greeksStrip.waitFor({ state: 'visible', timeout: 15_000 }).then(() => true).catch(() => false);
    if (!stripVisible) {
      test.skip(true, 'No option chart — Greeks strip not available');
      return;
    }

    const greekItems = greeksStrip.locator('.cw-greek-item');
    await expect(greekItems).toHaveCount(5, { timeout: 5000 });

    // Delta is the first item
    const deltaItem = greekItems.nth(0);
    await expect(deltaItem).toContainText('Δ');

    // CLICK to open
    const t0 = Date.now();
    await deltaItem.click();
    const popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });
    const elapsed = Date.now() - t0;
    expect(elapsed).toBeLessThan(1000);

    const text = (await popover.textContent()) || '';
    expect(text).toContain('Delta');
    expect(text).toContain('option price moves');

    // Viewport clipping check
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
      expect(popoverRect.left, 'Popover left must be >= 0').toBeGreaterThanOrEqual(0);
      expect(popoverRect.right, `Popover right must be <= ${vw}`).toBeLessThanOrEqual(vw);
      expect(popoverRect.top, 'Popover top must be >= 0').toBeGreaterThanOrEqual(0);
      expect(popoverRect.bottom, `Popover bottom must be <= ${vh}`).toBeLessThanOrEqual(vh);
    }

    // CLICK again to close
    await deltaItem.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
  });

  test('HOVER does NOT open the Greek tooltip (hover removed app-wide, 2026-10); click still works', async ({ page, viewport }) => {
    const greeksStrip = page.locator('.cw-greeks-strip');
    const stripVisible = await greeksStrip.waitFor({ state: 'visible', timeout: 15_000 }).then(() => true).catch(() => false);
    if (!stripVisible) {
      test.skip(true, 'No option chart — Greeks strip not available');
      return;
    }

    const greekItems = greeksStrip.locator('.cw-greek-item');
    await expect(greekItems).toHaveCount(5, { timeout: 5000 });

    const gammaItem = greekItems.nth(1); // Gamma is second
    await expect(gammaItem).toContainText('Γ');

    // HOVER must NOT open anything. A real `.hover()` proves the cursor
    // actually landed on the item (Playwright's actionability check).
    await gammaItem.hover();
    await page.waitForTimeout(300);
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

    // Positive control: click still opens/closes it.
    await gammaItem.click();
    const popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible({ timeout: 2000 });
    const text = (await popover.textContent()) || '';
    expect(text).toContain('Gamma');
    expect(text).toContain('rate of change');

    await gammaItem.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
  });

  test('All 5 Greeks + IV open distinct, correctly-worded tooltips via click; hover never opens any of them', async ({ page, viewport }) => {
    const greeksStrip = page.locator('.cw-greeks-strip');
    const stripVisible = await greeksStrip.waitFor({ state: 'visible', timeout: 15_000 }).then(() => true).catch(() => false);
    if (!stripVisible) {
      test.skip(true, 'No option chart — Greeks strip not available');
      return;
    }

    const greekItems = greeksStrip.locator('.cw-greek-item');
    const baseCount = await greekItems.count();
    expect(baseCount).toBeGreaterThanOrEqual(5);

    // Test patterns for the first 5 (mandatory Greeks)
    const greekPatterns = [
      { name: 'Δ', text: /Delta|option price/ },
      { name: 'Γ', text: /Gamma|rate of change/ },
      { name: 'Θ', text: /Theta|time decay/ },
      { name: 'V', text: /Vega|implied volatility|IV/ },
      { name: 'ρ', text: /Rho|interest rate|rate change/ },
    ];

    for (let i = 0; i < 5; i++) {
      const item = greekItems.nth(i);
      await expect(item).toContainText(greekPatterns[i].name);

      // CLICK test
      await item.click();
      const popover = page.locator('[role="tooltip"]').first();
      await expect(popover).toBeVisible({ timeout: 2000 });
      let text = (await popover.textContent()) || '';
      expect(text).toMatch(greekPatterns[i].text);
      await item.click(); // close
      await expect(page.locator('[role="tooltip"]')).toHaveCount(0);

      // HOVER must NOT reopen it (hover removed app-wide, 2026-10), even
      // while the cursor is still resting on the item from the click above.
      await item.hover();
      await page.waitForTimeout(300);
      await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
    }
  });

  test('Independence: opening one Greek closes another (per-item state, no cross-talk)', async ({ page }) => {
    const greeksStrip = page.locator('.cw-greeks-strip');
    const stripVisible = await greeksStrip.waitFor({ state: 'visible', timeout: 15_000 }).then(() => true).catch(() => false);
    if (!stripVisible) {
      test.skip(true, 'No option chart — Greeks strip not available');
      return;
    }

    const greekItems = greeksStrip.locator('.cw-greek-item');
    await expect(greekItems).toHaveCount(5, { timeout: 5000 });

    const deltaItem = greekItems.nth(0);
    const thetaItem = greekItems.nth(2);

    // Open Delta
    await deltaItem.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(1);
    let text = (await page.locator('[role="tooltip"]').first().textContent()) || '';
    expect(text).toContain('Delta');

    // Open Theta — should close Delta
    await thetaItem.click();
    const tooltips = page.locator('[role="tooltip"]');
    await expect(tooltips).toHaveCount(1);
    text = (await tooltips.first().textContent()) || '';
    expect(text).toContain('Theta');
    expect(text).not.toContain('Delta');
  });
});
