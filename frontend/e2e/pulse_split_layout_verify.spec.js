/**
 * Verifies the MarketPulse /pulse page split layout after commit 53630ef0.
 * - Desktop (1440x900): .mp-grid-left and .mp-grid-right should be side-by-side
 *   on the same row (top values within 10px), each ~50% wide.
 * - Mobile (800x900): grids should stack vertically (different top values).
 */

import { test, expect } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

/** Inject a JWT directly rather than driving the /signin form (faster, no form timeout risk). */
async function injectAuth(page) {
  let tok = null;
  for (const user of ['ambore', 'rambo']) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: user, password: PASS },
    });
    if (r.ok()) {
      tok = (await r.json()).access_token;
      break;
    }
  }
  if (!tok) throw new Error(`login failed against ${BASE}`);
  await page.context().addInitScript((t) => { sessionStorage.setItem('ramboq_token', t); }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });
}

test.describe('MarketPulse /pulse split layout', () => {

  test('desktop 1440x900 — grids are side-by-side', async ({ page }) => {
    test.setTimeout(90_000);

    await page.setViewportSize({ width: 1440, height: 900 });
    await injectAuth(page);

    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    // Wait for both grid containers to appear in the DOM
    await page.waitForSelector('.mp-grid-left', { state: 'visible', timeout: 30000 });
    await page.waitForSelector('.mp-grid-right', { state: 'visible', timeout: 30000 });

    // Allow ag-Grid to finish rendering
    await page.waitForLoadState('networkidle');

    // Collect layout metrics
    const m = await page.evaluate(() => {
      const left  = document.querySelector('.mp-grid-left');
      const right = document.querySelector('.mp-grid-right');
      const wrap  = document.querySelector('.mp-grids');

      const lr = left?.getBoundingClientRect();
      const rr = right?.getBoundingClientRect();
      const ws = wrap  ? getComputedStyle(wrap)  : null;
      const rs = right ? getComputedStyle(right) : null;

      return {
        leftTop:       lr?.top,
        rightTop:      rr?.top,
        leftWidth:     lr?.width,
        rightWidth:    rr?.width,
        leftLeft:      lr?.left,
        rightLeft:     rr?.left,
        flexDirection: ws?.flexDirection,
        rightFlex:     rs?.flex,
        rightWidthCSS: rs?.width,
        viewport:      window.innerWidth,
      };
    });

    const topDiff = Math.abs((m.leftTop ?? 0) - (m.rightTop ?? 0));

    console.log('=== Desktop 1440x900 layout metrics ===');
    console.log(`Viewport: ${m.viewport}px`);
    console.log(`Left  — top: ${m.leftTop?.toFixed(1)}, width: ${m.leftWidth?.toFixed(1)}, left: ${m.leftLeft?.toFixed(1)}`);
    console.log(`Right — top: ${m.rightTop?.toFixed(1)}, width: ${m.rightWidth?.toFixed(1)}, left: ${m.rightLeft?.toFixed(1)}`);
    console.log(`.mp-grids  flex-direction:  "${m.flexDirection}"`);
    console.log(`.mp-grid-right flex:        "${m.rightFlex}"`);
    console.log(`.mp-grid-right width (CSS): "${m.rightWidthCSS}"`);
    console.log(`Top diff: ${topDiff.toFixed(1)}px — same row: ${topDiff <= 10}`);

    // Screenshot before assertions so we always get it
    await page.screenshot({ path: 'test-results/pulse_desktop_split_layout.png', fullPage: false });
    console.log('Screenshot saved: test-results/pulse_desktop_split_layout.png');

    // Assertions
    expect(m.flexDirection, '.mp-grids flex-direction should be row at 1440px').toBe('row');
    expect(topDiff, `grids should share the same row (top diff ${topDiff.toFixed(1)}px ≤ 10px)`).toBeLessThanOrEqual(10);
    expect(m.leftWidth  ?? 0, 'left grid should be ≥ 400px wide').toBeGreaterThan(400);
    expect(m.rightWidth ?? 0, 'right grid should be ≥ 400px wide').toBeGreaterThan(400);
    // Right grid must start to the right of left grid
    expect(m.rightLeft ?? 0, 'right grid should start right of left grid').toBeGreaterThan((m.leftLeft ?? 0) + 200);
  });

  test('mobile 800x900 — grids stack vertically', async ({ page }) => {
    test.setTimeout(90_000);

    await page.setViewportSize({ width: 800, height: 900 });
    await injectAuth(page);

    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    await page.waitForSelector('.mp-grid-left',  { state: 'visible', timeout: 30000 });
    await page.waitForSelector('.mp-grid-right', { state: 'visible', timeout: 30000 });
    await page.waitForLoadState('networkidle');

    const m = await page.evaluate(() => {
      const left  = document.querySelector('.mp-grid-left');
      const right = document.querySelector('.mp-grid-right');
      const wrap  = document.querySelector('.mp-grids');

      const lr = left?.getBoundingClientRect();
      const rr = right?.getBoundingClientRect();
      const ws = wrap ? getComputedStyle(wrap) : null;

      return {
        leftTop:       lr?.top,
        rightTop:      rr?.top,
        leftWidth:     lr?.width,
        rightWidth:    rr?.width,
        flexDirection: ws?.flexDirection,
        viewport:      window.innerWidth,
      };
    });

    const topDiff = Math.abs((m.leftTop ?? 0) - (m.rightTop ?? 0));

    console.log('=== Mobile 800x900 layout metrics ===');
    console.log(`Viewport: ${m.viewport}px`);
    console.log(`Left  — top: ${m.leftTop?.toFixed(1)}, width: ${m.leftWidth?.toFixed(1)}`);
    console.log(`Right — top: ${m.rightTop?.toFixed(1)}, width: ${m.rightWidth?.toFixed(1)}`);
    console.log(`.mp-grids flex-direction: "${m.flexDirection}"`);
    console.log(`Top diff: ${topDiff.toFixed(1)}px — stacked: ${topDiff > 10}`);

    await page.screenshot({ path: 'test-results/pulse_mobile_stacked_layout.png', fullPage: false });
    console.log('Screenshot saved: test-results/pulse_mobile_stacked_layout.png');

    // Below 1024px breakpoint flex-direction should be column
    expect(m.flexDirection, '.mp-grids flex-direction should be column at 800px').toBe('column');
    expect(topDiff, `grids should be stacked (top diff ${topDiff.toFixed(1)}px > 10px)`).toBeGreaterThan(10);
  });
});
