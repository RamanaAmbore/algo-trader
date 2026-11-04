/**
 * chart_toolbar_header_strip.spec.js — 2026-11-03, operator: "the elements
 * to configure the chart should be in a header strip" + "some overlapping
 * borders have round corners and some don't which makes [it] clumsy".
 *
 * Before this fix, `.cw-header` (the row hosting the type/symbol/chart-type
 * pickers plus the intraday/range/indicators controls) had no background
 * or border of its own — every control floated directly on the chart's
 * dark background with its own independent border treatment: amber on
 * Select/SymbolSearchInput, cyan on the Intraday/Signals chips, neutral
 * white on the range-pill group; 3px radius everywhere except the
 * Intraday/Signals chips, which were 4px. Fixed by giving `.cw-header`
 * the same strip background + bottom-divider convention CardHeader
 * already uses app-wide, and normalizing the two locally-owned chips
 * (Intraday/Signals) to the same neutral 3px/rgba(255,255,255,0.08)
 * resting border every other toolbar control uses.
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;

test.use({ projects: ['chromium-desktop'] });

test('chart toolbar reads as one header strip, not a cluster of mismatched borders', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/charts', { waitUntil: 'domcontentloaded' });

  const header = page.locator('.cw-header').first();
  await expect(header).toBeVisible({ timeout: TIMEOUT });

  const headerStyle = await header.evaluate((el) => {
    const cs = getComputedStyle(el);
    return { background: cs.backgroundImage, borderBottomWidth: cs.borderBottomWidth, borderBottomStyle: cs.borderBottomStyle };
  });
  // A real background (gradient), not "none" — this is the strip itself.
  expect(headerStyle.background).not.toBe('none');
  expect(headerStyle.borderBottomWidth).toBe('1px');
  expect(headerStyle.borderBottomStyle).toBe('solid');

  const intraday = page.locator('.cw-intraday-btn').first();
  await expect(intraday).toBeVisible({ timeout: TIMEOUT });
  const intradayStyle = await intraday.evaluate((el) => {
    const cs = getComputedStyle(el);
    return { radius: cs.borderTopLeftRadius, color: cs.borderColor };
  });
  // Normalized to the SAME radius as the Select triggers / range group
  // (3px), not the old mismatched 4px.
  expect(intradayStyle.radius).toBe('3px');
  // Resting border is neutral translucent white, not cyan — cyan is
  // reserved for the active state only (class:active toggles it).
  expect(intradayStyle.color).toMatch(/rgba\(255,\s*255,\s*255,\s*0\.08\)/);

  const rangeGroup = page.locator('.cw-range-group').first();
  await expect(rangeGroup).toBeVisible({ timeout: TIMEOUT });
  const rangeGroupRadius = await rangeGroup.evaluate((el) => getComputedStyle(el).borderTopLeftRadius);
  // The intraday chip's radius now matches the range group's — one
  // consistent corner treatment across the strip's own local controls.
  expect(rangeGroupRadius).toBe(intradayStyle.radius);
});
