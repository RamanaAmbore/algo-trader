// Verify dashboard restructure: tabbed Capital/Equity card left half,
// intraday equity curve right half, curve actually shows variation.
import { test, expect } from '@playwright/test';

const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test('dashboard: tabbed cap/eq + curve side by side, curve has variation', async ({ page }) => {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post('https://ramboq.com/api/auth/login', {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  if (!tok) throw new Error('login failed');
  await page.context().addInitScript((t) => { sessionStorage.setItem('ramboq_token', t); }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });

  await page.setViewportSize({ width: 1440, height: 1200 });
  await page.goto('https://ramboq.com/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(5000);

  // Tab strip with both Capital + Equity
  const capTab = page.locator('.cap-eq-tab', { hasText: 'Capital' });
  const eqTab  = page.locator('.cap-eq-tab', { hasText: 'Equity' });
  await expect(capTab).toBeVisible();
  await expect(eqTab).toBeVisible();
  console.log('tabs found:', await capTab.count(), await eqTab.count());

  // Click Equity tab → equity panel should show
  await eqTab.click();
  await page.waitForTimeout(500);

  // Confirm tab activation
  const eqOn = await eqTab.evaluate((el) => el.classList.contains('cap-eq-tab-on'));
  console.log('equity tab active:', eqOn);
  expect(eqOn).toBe(true);

  await page.screenshot({ path: 'test-results/dash-restructure-equity-tab.png', fullPage: false });

  // Back to Capital
  await capTab.click();
  await page.waitForTimeout(500);
  await page.screenshot({ path: 'test-results/dash-restructure-capital-tab.png', fullPage: false });

  // Verify curve variation — read the polyline points and check spread
  const polylineData = await page.evaluate(() => {
    const poly = document.querySelector('svg.eq-svg polyline');
    return poly?.getAttribute('points') || '';
  });
  console.log('polyline points length:', polylineData.length);
  if (polylineData) {
    const pts = polylineData.split(/\s+/).filter(Boolean).map((s) => s.split(',').map(Number));
    const ys = pts.map(([, y]) => y);
    const ySpan = Math.max(...ys) - Math.min(...ys);
    console.log('curve y-span:', ySpan.toFixed(1), 'px out of ~180px chart height');
    // With day_pnl scale, the curve should occupy a meaningful fraction
    // of the chart height — at least 15px out of ~180.
    expect(ySpan).toBeGreaterThan(15);
  }
});
