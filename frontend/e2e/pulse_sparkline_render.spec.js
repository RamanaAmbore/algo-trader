// Sparkline column on /pulse should populate after fetchSparklines arrives.
// Two bugs were involved before the fix:
//   1. ag-Grid wasn't told to refresh its Curve cells when the reactive
//      `sparklines` $state map updated → cells stayed em-dashed forever.
//   2. /pulse routinely has >100 rows but the backend caps the batch at
//      100 symbols, so the single fetchSparklines call 400'd and the
//      map never got populated → cells stayed em-dashed for a *different*
//      reason. Fix: chunk into ≤100-symbol requests.
import { test, expect } from '@playwright/test';

test.setTimeout(90000);

const USER = process.env.PLAYWRIGHT_USER || 'rambo';
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
  await page.locator('.ag-row').first().waitFor({ timeout: 15000 });

  // First populated SVG → the refresh + chunking pipeline worked.
  await page.locator('.spark-cell svg polyline').first().waitFor({ timeout: 45000 });

  const svgCount = await page.locator('.spark-cell svg polyline').count();
  expect(svgCount).toBeGreaterThan(0);
});
