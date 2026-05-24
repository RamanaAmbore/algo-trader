// Visual verification for the /dashboard restructure (Capital + Equity
// buckets, full-width equity-curve hero, tabbed Positions / Holdings).
//
// Per CLAUDE.md / MEMORY rule: every frontend change ships with a
// Playwright spec against the deployed app. Default headless, --workers=1.
//
// Login: try 'ambore' (admin) first, fall back to 'rambo' (designated).
import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const PASS = process.env.PASS || 'rambo';

async function login(page) {
  await page.goto(`${BASE}/signin`);
  await page.fill('input[name="username"], input[type="text"]', 'ambore');
  await page.fill('input[type="password"]', PASS);
  await page.click('button:has-text("Sign In")');
  // designated → /admin/options, others → /pulse. Force /dashboard:
  await page.waitForLoadState('networkidle');
  await page.goto(`${BASE}/dashboard`);
  await page.waitForLoadState('networkidle');
}

test('desktop: buckets visible + Equity tab toggles', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await login(page);

  // Capital + Equity buckets render side-by-side
  await expect(page.locator('.bucket-cap')).toBeVisible();
  await expect(page.locator('.bucket-eq')).toBeVisible();

  // Equity card has two tabs with count chips
  const posTab = page.locator('.eq-tab:has-text("Positions")');
  const holTab = page.locator('.eq-tab:has-text("Holdings")');
  await expect(posTab).toBeVisible();
  await expect(holTab).toBeVisible();

  // Positions tab is active by default (amber)
  await expect(posTab).toHaveClass(/eq-tab-on/);

  // Click Holdings — it becomes active, Positions deactivates
  await holTab.click();
  await expect(holTab).toHaveClass(/eq-tab-on/);
  await expect(posTab).not.toHaveClass(/eq-tab-on/);

  // Equity curve still claims full width above
  const curve = page.locator('.row1-col-chart');
  await expect(curve).toBeVisible();

  await page.screenshot({ path: 'test-results/dashboard-buckets-desktop.png', fullPage: true });
});

test('mobile: buckets stack vertically', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);

  await expect(page.locator('.bucket-cap')).toBeVisible();
  await expect(page.locator('.bucket-eq')).toBeVisible();

  // On mobile the buckets stack — Capital sits above Equity in the DOM
  const capBox = await page.locator('.bucket-cap').boundingBox();
  const eqBox  = await page.locator('.bucket-eq').boundingBox();
  expect(capBox).toBeTruthy();
  expect(eqBox).toBeTruthy();
  expect(eqBox.y).toBeGreaterThan(capBox.y + capBox.height - 10);

  await page.screenshot({ path: 'test-results/dashboard-buckets-mobile.png', fullPage: true });
});
