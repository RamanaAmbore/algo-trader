// Sparkline column on /pulse should populate after fetchSparklines arrives.
// Before the fix, the Curve column rendered `—` placeholders that never
// updated because ag-Grid wasn't told to refresh its cells when the
// reactive `sparklines` map changed.
import { test, expect } from '@playwright/test';

const USER = 'ambore';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

async function signIn(page) {
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="username"], input#username, input#s-user').first().fill(USER);
  await page.locator('input[name="password"], input#password, input#s-pass').first().fill(PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
  for (let i = 0; i < 10; i++) {
    const has = await page.evaluate(() => !!sessionStorage.getItem('ramboq_token'));
    if (has) break;
    await new Promise((r) => setTimeout(r, 300));
  }
}

test('/pulse Curve column renders sparkline SVGs after data loads', async ({ page }) => {
  await signIn(page);
  await page.goto('/pulse', { waitUntil: 'networkidle' });

  // Grid mounts → wait for rows
  await page.locator('.ag-row').first().waitFor({ timeout: 15000 });

  // Sparkline poll fires immediately after positions/holdings load.
  // Give it up to 20s for the batch request to come back and for the
  // refresh effect to repaint cells.
  const sparkSvg = page.locator('.spark-cell svg polyline').first();
  await sparkSvg.waitFor({ timeout: 20000 });

  // Count: how many Curve cells now have an SVG vs an em-dash placeholder.
  const svgCount = await page.locator('.spark-cell svg polyline').count();
  const dashCount = await page.locator('.spark-cell span', { hasText: '—' }).count();
  console.log(`[/pulse] sparkline SVGs: ${svgCount}, em-dash placeholders: ${dashCount}`);

  // Real assertion: at least one populated SVG. Em-dashes are allowed
  // (some rows legitimately have no daily history — fresh listings,
  // illiquid instruments).
  expect(svgCount).toBeGreaterThan(0);
});
