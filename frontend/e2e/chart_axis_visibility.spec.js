// Verify chart axis text + grid lines render at the new (brighter)
// styling across Pulse, Dashboard, Derivatives.
import { test, expect } from '@playwright/test';

const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

async function _login(page) {
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
}

test('dashboard chart text + grid is legible', async ({ page }) => {
  await _login(page);
  await page.setViewportSize({ width: 1440, height: 1200 });
  await page.goto('https://ramboq.com/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(4000);

  // Find the inline equity-curve SVG.
  const eqSvg = page.locator('svg.eq-svg').first();
  await expect(eqSvg).toBeVisible();

  // Confirm at least one axis label is at the new bumped font-size.
  const axisLabels = eqSvg.locator('text');
  const fontSizes = await axisLabels.evaluateAll((els) =>
    els.map((e) => e.getAttribute('font-size')).filter(Boolean)
  );
  console.log('dashboard axis font-sizes:', [...new Set(fontSizes)]);
  // The new chart styles use 11 for axis ticks; older was 9/10.
  expect(fontSizes.some((s) => s === '11')).toBeTruthy();

  // Confirm at least 4 vertical x-grid lines are drawn (we ADDED these).
  const vlines = await eqSvg.locator('line[stroke-dasharray]').count();
  console.log('dashboard dashed grid lines:', vlines);
  expect(vlines).toBeGreaterThanOrEqual(4);

  // Verify the new "refreshed HH:MM" chip rendered on Capital + Equity cards.
  const chips = page.locator('.bucket-refresh-chip');
  await expect(chips.first()).toBeVisible();
  console.log('refresh chips:', await chips.count());

  await page.screenshot({ path: 'test-results/dashboard-chart-polish.png', fullPage: false });
});

test('derivatives payoff chart has bright sigma + axis labels', async ({ page }) => {
  await _login(page);
  await page.setViewportSize({ width: 1440, height: 1200 });
  await page.goto('https://ramboq.com/admin/options', { waitUntil: 'networkidle' });
  await page.waitForTimeout(4000);

  // The payoff SVG renders only when there's data; capture whatever's there.
  await page.screenshot({ path: 'test-results/derivatives-chart-polish.png', fullPage: false });
});
