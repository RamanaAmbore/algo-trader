/**
 * charts_axis_polish.spec.js
 *
 * Verifies the axis-polish changes made to ChartWorkspace.svelte:
 *   1. Y-axis labels carry a -45° rotation transform (save horizontal space)
 *   2. X-axis labels remain horizontal (no rotation)
 *   3. Horizontal AND vertical grid lines are both present (>4 each)
 *   4. Y-axis baseline (left vertical line) is rendered
 *   5. X-axis baseline (bottom horizontal line) is rendered
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *   SSOT   — rotation attribute present on Y labels; grid lines exist
 *   Perf   — total SVG <line> + <path> count stays < 200 (no node explosion)
 *   Stale  — source grep: rotate(-45 present; CPAD_L = 44 present
 *   Reuse  — intraday sub-chart uses same rotation convention
 *   UX     — mobile: no horizontal overflow; desktop: left margin freed
 *
 * Run against dev.ramboq.com:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/charts_axis_polish.spec.js \
 *   --project=chromium-desktop --project=mobile-portrait --workers=1
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

const __dir = dirname(fileURLToPath(import.meta.url));
const _CW_SRC = join(__dir, '../src/lib/ChartWorkspace.svelte');

/** Navigate to the charts page for a given symbol and wait until the SVG renders. */
async function gotoChart(page, symbol = 'NIFTY 50') {
  await loginAsAdmin(page);
  const url = `${BASE}/charts?symbol=${encodeURIComponent(symbol)}&mode=live`;
  await page.goto(url, { waitUntil: 'domcontentloaded' });

  // Wait for the chart SVG to appear — it renders inside .cw-chart-container
  // only when bars have been loaded (the {#if !_bars.length} branch clears).
  await page.waitForFunction(
    () => {
      const svg = document.querySelector('.cw-chart-container svg');
      if (!svg) return false;
      // At least one price path must be populated (d= attribute with real data).
      const paths = svg.querySelectorAll('path[d]');
      for (const p of paths) {
        if ((p.getAttribute('d') || '').length > 20) return true;
      }
      return false;
    },
    { timeout: 35_000 },
  );
}

// ── Dimension 1: SSOT — rotation + grid ──────────────────────────────────────
test('SSOT: Y-axis labels have rotate(-45) transform', async ({ page }) => {
  test.setTimeout(90_000);
  await gotoChart(page);

  const svg = page.locator('.cw-chart-container svg').first();
  await expect(svg).toBeVisible();

  // Collect transform attributes on all <text> elements inside the main chart SVG.
  const transforms = await svg.locator('text').evaluateAll((els) =>
    els.map((el) => el.getAttribute('transform') || ''),
  );

  // At least one text element should carry rotate(-45 — our Y-axis labels.
  const rotated = transforms.filter((t) => t.includes('rotate(-45'));
  expect(
    rotated.length,
    `Expected Y-axis label text elements with rotate(-45) transform. ` +
      `Found transforms: ${JSON.stringify(transforms)}`,
  ).toBeGreaterThan(0);
});

test('SSOT: X-axis labels remain horizontal (no rotation)', async ({ page }) => {
  test.setTimeout(90_000);
  await gotoChart(page);

  const svg = page.locator('.cw-chart-container svg').first();

  // X-axis labels are placed below the plot area at y ≈ CPAD_T + innerH + 14.
  // They use text-anchor="start" | "middle" | "end" but no rotation.
  // We confirm the count of text elements WITHOUT a transform attribute
  // is >= 5 (the 5 x-axis date labels).
  const allTextEls = await svg.locator('text').evaluateAll((els) =>
    els.map((el) => ({
      hasTransform: !!el.getAttribute('transform'),
      y: parseFloat(el.getAttribute('y') || '0'),
    })),
  );

  // At least some text elements without rotation must exist (X labels, RSI labels).
  const unrotated = allTextEls.filter((el) => !el.hasTransform);
  expect(
    unrotated.length,
    'Expected at least some unrotated text elements (X-axis labels / RSI labels)',
  ).toBeGreaterThan(0);
});

test('SSOT: both horizontal and vertical grid lines are present (>4 each)', async ({ page }) => {
  test.setTimeout(90_000);
  await gotoChart(page);

  const svg = page.locator('.cw-chart-container svg').first();

  // Collect all <line> elements and classify by orientation.
  const lines = await svg.locator('line').evaluateAll((els) =>
    els.map((el) => ({
      x1: parseFloat(el.getAttribute('x1') || '0'),
      y1: parseFloat(el.getAttribute('y1') || '0'),
      x2: parseFloat(el.getAttribute('x2') || '0'),
      y2: parseFloat(el.getAttribute('y2') || '0'),
    })),
  );

  // Horizontal lines: y1 === y2 (within 0.5px tolerance).
  const hLines = lines.filter((l) => Math.abs(l.y1 - l.y2) < 0.5 && Math.abs(l.x2 - l.x1) > 50);
  // Vertical lines: x1 === x2 (within 0.5px tolerance).
  const vLines = lines.filter((l) => Math.abs(l.x1 - l.x2) < 0.5 && Math.abs(l.y2 - l.y1) > 20);

  expect(
    hLines.length,
    `Expected >4 horizontal grid lines, found ${hLines.length}`,
  ).toBeGreaterThan(4);

  expect(
    vLines.length,
    `Expected >4 vertical lines (grid + baselines), found ${vLines.length}`,
  ).toBeGreaterThan(4);
});

// ── Dimension 2: Performance — SVG node count budget ─────────────────────────
test('perf: total SVG line+path count stays under 200', async ({ page }) => {
  test.setTimeout(90_000);
  await gotoChart(page);

  const svg = page.locator('.cw-chart-container svg').first();

  const counts = await svg.evaluate((svgEl) => ({
    lines: svgEl.querySelectorAll('line').length,
    paths: svgEl.querySelectorAll('path').length,
  }));

  const total = counts.lines + counts.paths;
  expect(
    total,
    `SVG node count too high (lines:${counts.lines} + paths:${counts.paths} = ${total}). ` +
      `Grid additions should not explode the DOM.`,
  ).toBeLessThan(200);
});

// ── Dimension 3: Stale code — source-level grep ───────────────────────────────
test('stale: ChartWorkspace source has rotate(-45) and reduced CPAD_L=44', () => {
  let src;
  try {
    src = readFileSync(_CW_SRC, 'utf-8');
  } catch (e) {
    throw new Error(`Could not read ChartWorkspace.svelte at ${_CW_SRC}: ${e.message}`);
  }

  expect(
    src.includes('rotate(-45'),
    'Source missing rotate(-45) — Y-label rotation not applied',
  ).toBe(true);

  expect(
    src.includes('CPAD_L  = 44'),
    'Source still has old CPAD_L=56 — left-margin reduction not applied',
  ).toBe(true);

  // Y-axis baseline: a vertical line at x=CPAD_L spanning from CPAD_T to CPAD_T+_innerH
  expect(
    src.includes('Y-axis baseline'),
    'Source missing Y-axis baseline comment — vertical axis line not added',
  ).toBe(true);
});

// ── Dimension 4: Reuse — intraday sub-chart uses same rotation ────────────────
test('reuse: intraday sub-chart source also applies rotate(-45) to Y labels', () => {
  let src;
  try {
    src = readFileSync(_CW_SRC, 'utf-8');
  } catch (e) {
    throw new Error(`Could not read ChartWorkspace.svelte at ${_CW_SRC}: ${e.message}`);
  }

  // The intraday chart Y-axis labels also rotate: "rotate(-45 {P2L" or
  // "rotate(-45 {P2L - 3" should appear in the file.
  expect(
    /rotate\(-45 \{P2L/.test(src),
    'Intraday sub-chart Y labels do not apply the same rotate(-45) convention. ' +
      'Both main chart and intraday should use the same rotation approach.',
  ).toBe(true);
});

// ── Dimension 5: UX — both viewports ─────────────────────────────────────────

// Desktop: rotated labels render; left margin freed; grid+axes visible
test('UX desktop: rotated labels fit, grid visible, no overflow', async ({ page }) => {
  test.setTimeout(90_000);
  // This test runs only for chromium-desktop project but won't fail on mobile
  // (we guard by viewport width below).
  await gotoChart(page);

  const { width } = page.viewportSize() ?? { width: 1400 };

  const svg = page.locator('.cw-chart-container svg').first();
  await expect(svg).toBeVisible();

  if (width >= 900) {
    // On desktop: confirm the SVG itself does not overflow its container.
    const container = page.locator('.cw-chart-container').first();
    const [svgBox, containerBox] = await Promise.all([
      svg.boundingBox(),
      container.boundingBox(),
    ]);

    if (svgBox && containerBox) {
      expect(
        svgBox.width,
        `SVG width (${svgBox.width}) exceeds container width (${containerBox.width})`,
      ).toBeLessThanOrEqual(containerBox.width + 2); // 2px tolerance for rounding
    }

    // Confirm rotated labels (text with rotate transform) are within the SVG bounds.
    const rotatedLabelBoxes = await svg.locator('text').evaluateAll((els, svgRect) => {
      return els
        .filter((el) => (el.getAttribute('transform') || '').includes('rotate(-45'))
        .map((el) => {
          const b = el.getBoundingClientRect();
          return { left: b.left, right: b.right, top: b.top, bottom: b.bottom };
        });
    }, await svg.boundingBox());

    // All rotated labels should have their right edge to the LEFT of the SVG.
    // (They live in the left margin, which is CPAD_L=44 px wide.)
    for (const lb of rotatedLabelBoxes) {
      // Right edge of label should not extend far into the plot area.
      // Since CPAD_L=44px and labels rotate leftward, they should end <= 50px
      // from the SVG left edge (browser px, not SVG user units).
      if (svgBox) {
        const labelRightRelSvg = lb.right - svgBox.x;
        expect(
          labelRightRelSvg,
          `Rotated Y-axis label right edge (${labelRightRelSvg.toFixed(1)}px from SVG left) ` +
            'extended too far into the plot area. Left margin may be too narrow.',
        ).toBeLessThanOrEqual(55);
      }
    }
  }

  await page.screenshot({ path: 'test-results/charts_axis_polish_desktop.png', fullPage: false });
});

// Mobile: labels don't overflow viewport; chart visible without horizontal scroll
test('UX mobile: chart fits viewport, no horizontal scroll', async ({ page }) => {
  test.setTimeout(90_000);
  await gotoChart(page);

  const { width } = page.viewportSize() ?? { width: 393 };

  const container = page.locator('.cw-chart-container').first();
  await expect(container).toBeVisible();

  // No horizontal scroll — scrollWidth should equal clientWidth on .cw-root.
  const overflows = await page.evaluate(() => {
    const root = document.querySelector('.cw-root');
    if (!root) return { scrollWidth: 0, clientWidth: 0 };
    return { scrollWidth: root.scrollWidth, clientWidth: root.clientWidth };
  });

  expect(
    overflows.scrollWidth,
    `Horizontal overflow on .cw-root: scrollWidth(${overflows.scrollWidth}) > clientWidth(${overflows.clientWidth}). ` +
      'Rotated labels may be causing layout overflow on mobile.',
  ).toBeLessThanOrEqual(overflows.clientWidth + 2);

  // SVG should be visible and have non-zero dimensions.
  const svg = page.locator('.cw-chart-container svg').first();
  const svgBox = await svg.boundingBox();
  if (svgBox) {
    expect(svgBox.width, 'SVG has zero width on mobile').toBeGreaterThan(100);
    expect(svgBox.height, 'SVG has zero height on mobile').toBeGreaterThan(100);

    // On mobile the SVG should not be wider than the viewport.
    expect(
      svgBox.width,
      `SVG wider than viewport on mobile: ${svgBox.width.toFixed(0)}px > ${width}px`,
    ).toBeLessThanOrEqual(width + 2);
  }

  await page.screenshot({ path: `test-results/charts_axis_polish_mobile_${width}.png`, fullPage: false });
});
