// Verify the firm NAV panel renders on /performance for anonymous
// visitors (no sign-in) against prod.
import { test, expect } from '@playwright/test';

test('public /performance shows firm NAV without sign-in', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  // Hit prod explicitly (the user requested prod verification).
  await page.goto('https://ramboq.com/performance', { waitUntil: 'networkidle' });
  await page.waitForTimeout(3000);

  // Look for the FIRM NAV label
  const firmLabel = page.locator('text=/firm.*nav/i').first();
  await expect(firmLabel).toBeVisible({ timeout: 10000 });

  // The dollar value should also be visible (formatted with ₹)
  const rupee = await page.locator('text=/₹.*[0-9]/').first().textContent();
  console.log('[public firm-nav visible value]', rupee);

  await page.screenshot({ path: 'test-results/public-firm-nav.png', fullPage: false });
});
