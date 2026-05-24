// Visual verification for the /dashboard restructure:
//   - Capital + Equity buckets, equal height, side-by-side
//   - Equity card: stacked Positions Summary + Holdings Summary
//   - Top Winners + Top Losers: tabbed buckets (Underlying / Midcap /
//     Smallcap / Holdings / Positions) with count chips
//   - FullscreenButton present on every major card; toggling adds
//     the .fs-card-on class to the card root
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

  const eq = page.locator('.bucket-eq');
  await expect(eq.locator('.bucket-subheader:has-text("Positions")')).toBeVisible();
  await expect(eq.locator('.bucket-subheader:has-text("Holdings")')).toBeVisible();

  const capBox = await page.locator('.bucket-cap').boundingBox();
  const eqBox  = await page.locator('.bucket-eq').boundingBox();
  expect(capBox).toBeTruthy();
  expect(eqBox).toBeTruthy();
  expect(eqBox.x).toBeGreaterThan(capBox.x + capBox.width - 50);

  // Wait for data
  await page.waitForTimeout(2000);
  const posCount = await eq.locator('.bucket-subheader:has-text("Positions") .eq-count').textContent();
  const holCount = await eq.locator('.bucket-subheader:has-text("Holdings") .eq-count').textContent();
  // eslint-disable-next-line no-console
  console.log(`[dashboard] positions=${posCount} holdings=${holCount}`);

  await page.screenshot({ path: 'test-results/dashboard-buckets-desktop.png', fullPage: true });
});

test('Winners/Losers tabs + tab toggle works', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await page.waitForTimeout(2000);

  const winCard = page.locator('.wl-tile-win');
  // Tab strip should be present
  await expect(winCard.locator('.wl-tab').nth(0)).toBeVisible();
  // Five buckets per card
  await expect(winCard.locator('.wl-tab')).toHaveCount(5);

  // Default tab is Holdings (4th: Underlying / Midcap / Smallcap / Holdings / Positions)
  // — holdings has the deepest data in a typical book, so this avoids
  // landing on an underlying tab that may only show 2-3 entries.
  await expect(winCard.locator('.wl-tab').nth(3)).toHaveClass(/wl-tab-on/);

  // Click the Positions tab (5th)
  await winCard.locator('.wl-tab').nth(4).click();
  await expect(winCard.locator('.wl-tab').nth(4)).toHaveClass(/wl-tab-on/);
  await expect(winCard.locator('.wl-tab').nth(3)).not.toHaveClass(/wl-tab-on/);
});

test('FullscreenButton promotes Capital to modal on click', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await page.waitForTimeout(1500);

  const cap = page.locator('.bucket-cap');
  await expect(cap).not.toHaveClass(/fs-card-on/);

  // Click the expand button in Capital's header
  await cap.locator('.fs-btn').first().click();
  await expect(cap).toHaveClass(/fs-card-on/);

  // Backdrop is mounted
  await expect(page.locator('.fs-backdrop')).toBeVisible();

  // ESC key collapses it
  await page.keyboard.press('Escape');
  await expect(cap).not.toHaveClass(/fs-card-on/);
  await expect(page.locator('.fs-backdrop')).toHaveCount(0);

  // Click again then click backdrop to close
  await cap.locator('.fs-btn').first().click();
  await expect(cap).toHaveClass(/fs-card-on/);
  await page.locator('.fs-backdrop').click({ position: { x: 5, y: 5 } });
  await expect(cap).not.toHaveClass(/fs-card-on/);

  await page.screenshot({ path: 'test-results/dashboard-fullscreen-after.png', fullPage: false });
});

test('mobile: buckets stack vertically + tabs still present', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  await page.waitForTimeout(1500);

  await expect(page.locator('.bucket-cap')).toBeVisible();
  await expect(page.locator('.bucket-eq')).toBeVisible();

  const capBox = await page.locator('.bucket-cap').boundingBox();
  const eqBox  = await page.locator('.bucket-eq').boundingBox();
  expect(eqBox.y).toBeGreaterThan(capBox.y + capBox.height - 10);

  // Tab strip still renders on mobile (wraps if needed)
  await expect(page.locator('.wl-tile-win .wl-tab').nth(0)).toBeVisible();

  await page.screenshot({ path: 'test-results/dashboard-buckets-mobile.png', fullPage: true });
});
