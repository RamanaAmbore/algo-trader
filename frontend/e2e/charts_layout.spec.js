/**
 * charts_layout.spec.js
 *
 * Verifies the post-Sprint-F+ ChartWorkspace row layout:
 *
 *   Row 1 (.cw-picker): type-filter Select + SymbolSearchInput + chart-type Select
 *   Row 2 (.cw-controls): Intraday btn + range group (1D…1Y) + Overlays MultiSelect
 *
 * Operator request: "move line to first row along with symbol. and then shift
 * overlay to 2nd row after 1Y. fix it."
 *
 * Interpretation documented in commit body:
 *   "line" = chart-type Select (Line/Area/Candle/Plot) → moved to .cw-picker
 *   "overlay" = Overlays MultiSelect (SMA/EMA/Bollinger/RSI) → moved to .cw-controls after 1Y
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *   1. SSOT     — series-type toggle and symbol picker share the same .cw-picker row
 *   2. Perf     — cold XHR budget ≤ 35 calls (inherits approved budget from chart_1y_and_overlay)
 *   3. Stale    — Overlays trigger is NOT inside .cw-chart-container (old position gone)
 *   4. Reusable — .cw-range-group class still present on row 2 controls row
 *   5. UX       — active range button still uses amber palette (#fbbf24) per CLAUDE.md
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/charts_layout.spec.js \
 *   --project=chromium-desktop --workers=1
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const CHART_URL = `${BASE}/charts?symbol=${encodeURIComponent('NIFTY 50')}&mode=live`;

test.describe('/charts — row layout after series-toggle + overlay move', () => {

  test('SSOT: series-type toggle and symbol picker are in the same row (.cw-picker)', async ({ page }) => {
    test.setTimeout(60_000);
    await loginAsAdmin(page);
    await page.goto(CHART_URL, { waitUntil: 'domcontentloaded' });

    // Wait until the picker row is rendered.
    await expect(page.locator('.cw-picker')).toBeVisible({ timeout: 20_000 });

    // The chart-type Select trigger must be a descendant of .cw-picker.
    // We identify it by ariaLabel="Chart type" set on the Select component.
    const chartTypeTrigger = page.locator('.cw-picker [aria-label="Chart type"], .cw-picker .rbq-select-trigger').first();
    await expect(
      chartTypeTrigger,
      'Chart-type Select trigger must be inside .cw-picker (row 1 with symbol)',
    ).toBeVisible({ timeout: 10_000 });

    // The SymbolSearchInput must also be inside .cw-picker.
    const symbolInput = page.locator('.cw-picker input, .cw-picker [aria-label="Symbol — pinned or search"]').first();
    await expect(
      symbolInput,
      'SymbolSearchInput must remain inside .cw-picker',
    ).toBeVisible({ timeout: 5_000 });

    // Both must share the same .cw-picker parent — confirm via DOM evaluate.
    const sameParent = await page.evaluate(() => {
      const picker = document.querySelector('.cw-picker');
      if (!picker) return false;
      // Chart-type trigger (ariaLabel set via ariaLabel prop on Select)
      const typeTrigger = picker.querySelector('[aria-label="Chart type"]');
      // Symbol input
      const symInput = picker.querySelector('input');
      return !!(typeTrigger && symInput);
    });
    expect(
      sameParent,
      'Both chart-type Select and symbol input must be descendants of .cw-picker',
    ).toBe(true);
  });

  test('SSOT: Overlays trigger is inside .cw-controls (row 2, after 1Y)', async ({ page }) => {
    test.setTimeout(60_000);
    await loginAsAdmin(page);
    await page.goto(CHART_URL, { waitUntil: 'domcontentloaded' });

    await expect(page.locator('.cw-controls')).toBeVisible({ timeout: 20_000 });

    // The Overlays MultiSelect trigger must be a descendant of .cw-controls.
    const overlayTrigger = page.locator('.cw-controls .cw-overlay-panel').first();
    await expect(
      overlayTrigger,
      'Overlays panel must be inside .cw-controls (row 2)',
    ).toBeVisible({ timeout: 10_000 });

    // The Overlays panel must appear AFTER the range group in DOM order.
    const orderCorrect = await page.evaluate(() => {
      const controls = document.querySelector('.cw-controls');
      if (!controls) return false;
      const rangeGroup = controls.querySelector('.cw-range-group');
      const overlayPanel = controls.querySelector('.cw-overlay-panel');
      if (!rangeGroup || !overlayPanel) return false;
      // Node.DOCUMENT_POSITION_FOLLOWING (4) means overlayPanel comes after rangeGroup.
      const pos = rangeGroup.compareDocumentPosition(overlayPanel);
      // eslint-disable-next-line no-bitwise
      return !!(pos & Node.DOCUMENT_POSITION_FOLLOWING);
    });
    expect(
      orderCorrect,
      'Overlays panel must appear after .cw-range-group in the .cw-controls DOM order',
    ).toBe(true);
  });

  test('Perf: cold XHR budget ≤ 35 API calls', async ({ page }) => {
    test.setTimeout(60_000);
    await loginAsAdmin(page);

    /** @type {string[]} */
    const apiCalls = [];
    page.on('request', (req) => {
      if (req.url().includes('/api/')) apiCalls.push(req.url());
    });

    await page.goto(CHART_URL, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 20_000 });
    // Allow one polling cycle to settle.
    await page.waitForTimeout(2_000);

    expect(
      apiCalls.length,
      `Cold-load XHR budget exceeded. Got ${apiCalls.length} calls:\n${apiCalls.join('\n')}`,
    ).toBeLessThanOrEqual(35);
  });

  test('Stale: Overlays trigger is NOT inside .cw-chart-container (old floating position gone)', async ({ page }) => {
    test.setTimeout(60_000);
    await loginAsAdmin(page);
    await page.goto(CHART_URL, { waitUntil: 'domcontentloaded' });

    await expect(page.locator('.cw-chart-container')).toBeVisible({ timeout: 20_000 });

    // The Overlays panel must NOT be a descendant of .cw-chart-container
    // (where it used to live as position:absolute top-right of the chart).
    const overlayInsideChart = await page.evaluate(() => {
      const chartContainer = document.querySelector('.cw-chart-container');
      if (!chartContainer) return false;
      return !!chartContainer.querySelector('.cw-overlay-panel');
    });
    expect(
      overlayInsideChart,
      'Overlays panel must not be inside .cw-chart-container — old floating position leaked back',
    ).toBe(false);

    // Also confirm the chart-type Select is NOT inside .cw-controls
    // (its old location before the move).
    const chartTypeInControls = await page.evaluate(() => {
      const controls = document.querySelector('.cw-controls');
      if (!controls) return false;
      return !!controls.querySelector('[aria-label="Chart type"]');
    });
    expect(
      chartTypeInControls,
      'Chart-type Select must not be inside .cw-controls — it was moved to .cw-picker (row 1)',
    ).toBe(false);
  });

  test('Reusable: .cw-range-group class present in row 2 (.cw-controls)', async ({ page }) => {
    test.setTimeout(60_000);
    await loginAsAdmin(page);
    await page.goto(CHART_URL, { waitUntil: 'domcontentloaded' });

    // The canonical range pill group must still live inside .cw-controls.
    const rangeInControls = page.locator('.cw-controls .cw-range-group');
    await expect(
      rangeInControls,
      '.cw-range-group must remain inside .cw-controls (row 2)',
    ).toBeVisible({ timeout: 20_000 });

    // All six range buttons must still be present.
    const rangeBtns = page.locator('.cw-controls .cw-range-group .cw-range-btn');
    await expect(rangeBtns).toHaveCount(6, { timeout: 5_000 });
  });

  test('UX: active range button uses amber palette; Overlays trigger has sky border', async ({ page }) => {
    test.setTimeout(60_000);
    await loginAsAdmin(page);
    await page.goto(CHART_URL, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.cw-range-group')).toBeVisible({ timeout: 20_000 });

    // Click the 1M range button to guarantee an active state.
    const btn1M = page.locator('.cw-range-group .cw-range-btn', { hasText: '1M' });
    await btn1M.click();
    // Wait for it to become active (class added synchronously in Svelte reactivity).
    await expect(btn1M).toHaveClass(/active/, { timeout: 5_000 });

    // Active range button should use the amber palette color (#fbbf24).
    // CLAUDE.md: "action = #fbbf24 amber"
    const activeColor = await btn1M.evaluate((el) => getComputedStyle(el).color);
    // rgb(251, 191, 36) = #fbbf24
    expect(
      activeColor,
      `Active range button color should be amber #fbbf24 (rgb(251,191,36)), got: ${activeColor}`,
    ).toBe('rgb(251, 191, 36)');

    // Overlays panel border must use sky-blue tint (rgba(125,211,252,0.32))
    // — the canonical info/sky palette for overlay controls.
    const overlayBorder = await page.locator('.cw-overlay-panel').evaluate(
      (el) => getComputedStyle(el).borderColor,
    );
    // Accept any rgba containing sky-blue values (125, 211, 252).
    expect(
      overlayBorder,
      `Overlays panel border should contain sky-blue palette (125,211,252), got: ${overlayBorder}`,
    ).toMatch(/125,\s*211,\s*252/);
  });
});
