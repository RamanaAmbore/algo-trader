// Verify the dashboard's Intraday/Performance tabbed card + the
// Capital/Equity header-timestamp removal + the chart x-axis label
// removal. Targets dev.ramboq.com by default; pass BASE_URL for prod.
//
// Run:
//   BASE_URL=https://dev.ramboq.com npx playwright test dashboard_tabbed_chart_verify.spec.js --workers=1 --project=chromium-desktop

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test(`dashboard tabbed chart + header cleanup [${BASE}]`, async ({ page }) => {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  if (!tok) throw new Error(`login failed against ${BASE}`);
  await page.context().addInitScript((t) => { sessionStorage.setItem('ramboq_token', t); }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/dashboard`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(3500);

  // (a) Row-1 right card now carries TWO tabs — Intraday + Performance —
  //     inside its header tab strip. The header used a .mp-section-label
  //     before; now it should host .cap-eq-tabs (same idiom as the
  //     Capital|Equity card on the left).
  const chartCard = page.locator('.row1-col-chart').first();
  await expect(chartCard).toBeVisible();
  const chartTabs = chartCard.locator('.cap-eq-tabs').first();
  await expect(chartTabs).toBeVisible();
  const tabButtons = chartTabs.locator('button');
  await expect(tabButtons).toHaveCount(2);
  await expect(tabButtons.nth(0)).toHaveText('Intraday');
  await expect(tabButtons.nth(1)).toHaveText('Performance');

  // (a) Default tab is Intraday — the SVG should be visible.
  const eqSvg = chartCard.locator('svg.eq-svg').first();
  // The empty-state div may render instead of SVG when no equity data
  // exists. Accept either: at least one of the two should be visible.
  const eqEmpty = chartCard.locator('.eq-empty').first();
  const intradayVisible = (await eqSvg.isVisible().catch(() => false))
                       || (await eqEmpty.isVisible().catch(() => false));
  expect(intradayVisible, 'Intraday panel is rendered by default').toBe(true);

  // (b) Click Performance — PnlAnalysis renders. Earlier the standalone
  //     P&L Analysis card had a "P&L ANALYSIS" mp-section-label; that
  //     should NOT exist anymore (the standalone full-width card was
  //     retired in favour of the tab).
  await tabButtons.nth(1).click();
  await page.waitForTimeout(500);
  // The intraday SVG should now be hidden (parent div has [hidden]).
  // PnlAnalysis-specific surfaces should be visible — e.g., the
  // benchmark range presets that PnlAnalysis renders.
  // Use a loose check: a Select / preset chip / "Benchmark" text appears.
  const pnlBody = chartCard.locator('.card-body').nth(1);
  await expect(pnlBody).toBeVisible();
  // The standalone old card is gone — assert no full-width
  // .dash-pnl-details container exists.
  await expect(page.locator('.dash-pnl-details')).toHaveCount(0);

  // (c) Switch back to Intraday tab.
  await tabButtons.nth(0).click();
  await page.waitForTimeout(300);

  await page.screenshot({
    path: `test-results/dashboard-chart-tabbed-${BASE.includes('dev') ? 'dev' : 'prod'}.png`,
  });

  // (d) Capital/Equity card header — bucket-refresh-chip is GONE.
  const capEqCard = page.locator('.cap-eq-tabbed').first();
  await expect(capEqCard).toBeVisible();
  await expect(capEqCard.locator('.bucket-refresh-chip')).toHaveCount(0);

  // (e) Intraday SVG: x-axis text labels removed. Earlier the chart
  //     rendered HH:MM text under the curve (text-anchor="middle" near
  //     y=CHART_H-6). We can probe by counting how many <text> elements
  //     sit in the bottom 10 px of the chart viewBox region — should be
  //     zero now. The y-axis labels render with text-anchor="start" on
  //     the RIGHT edge so they don't interfere.
  if (await eqSvg.isVisible().catch(() => false)) {
    const bottomTextCount = await eqSvg.locator('text[text-anchor="middle"]').count();
    expect(bottomTextCount, 'no x-axis HH:MM text labels on intraday chart').toBe(0);
  }
});
