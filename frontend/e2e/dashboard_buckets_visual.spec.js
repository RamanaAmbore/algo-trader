// Visual verification for the /dashboard restructure (Capital + Equity
// buckets, full-width equity-curve hero, stacked Positions / Holdings
// summary inside the Equity card).
//
// Per CLAUDE.md / MEMORY rule: every frontend change ships with a
// Playwright spec against the deployed app. Default headless, --workers=1.
//
// Run:
//   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com PLAYWRIGHT_PASS=<rambo-pass> \
//     npx playwright test e2e/dashboard_buckets_visual.spec.js \
//     --project=chromium-desktop --workers=1
import { test, expect } from '@playwright/test';

const USER = 'rambo';
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
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
}

test('desktop: Capital + Equity render side-by-side, both summaries stacked', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);

  await expect(page.locator('.bucket-cap')).toBeVisible();
  await expect(page.locator('.bucket-eq')).toBeVisible();

  // Equity card now has both sub-headings (no tabs)
  const eq = page.locator('.bucket-eq');
  await expect(eq.locator('.bucket-subheader:has-text("Positions")')).toBeVisible();
  await expect(eq.locator('.bucket-subheader:has-text("Holdings")')).toBeVisible();

  // Count chips render next to each sub-heading
  await expect(eq.locator('.eq-count').nth(0)).toBeVisible();
  await expect(eq.locator('.eq-count').nth(1)).toBeVisible();

  // Equity curve full-width above the buckets
  await expect(page.locator('.row1-col-chart')).toBeVisible();

  // Capital + Equity horizontally adjacent at 1440 width
  const capBox = await page.locator('.bucket-cap').boundingBox();
  const eqBox  = await page.locator('.bucket-eq').boundingBox();
  expect(capBox).toBeTruthy();
  expect(eqBox).toBeTruthy();
  expect(eqBox.x).toBeGreaterThan(capBox.x + capBox.width - 50);

  // Wait a couple of polling cycles for positions/holdings to populate
  await page.waitForTimeout(2000);

  // If the response-shape bug is regressed, count chips will be "0".
  // Print whatever the chip shows so the screenshot artefact carries
  // the actual value an operator would see on the deployed app.
  const posCount = await eq.locator('.bucket-subheader:has-text("Positions") .eq-count').textContent();
  const holCount = await eq.locator('.bucket-subheader:has-text("Holdings") .eq-count').textContent();
  // eslint-disable-next-line no-console
  console.log(`[dashboard] positions=${posCount} holdings=${holCount}`);

  await page.screenshot({ path: 'test-results/dashboard-buckets-desktop.png', fullPage: true });
});

test('mobile: buckets stack vertically + stacked Equity sub-sections', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);

  await expect(page.locator('.bucket-cap')).toBeVisible();
  await expect(page.locator('.bucket-eq')).toBeVisible();

  const capBox = await page.locator('.bucket-cap').boundingBox();
  const eqBox  = await page.locator('.bucket-eq').boundingBox();
  expect(capBox).toBeTruthy();
  expect(eqBox).toBeTruthy();
  expect(eqBox.y).toBeGreaterThan(capBox.y + capBox.height - 10);

  await page.screenshot({ path: 'test-results/dashboard-buckets-mobile.png', fullPage: true });
});
