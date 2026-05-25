// Verify CollapseButton toggles + persists per user.
import { test, expect } from '@playwright/test';

const USER = 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

async function signIn(page) {
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"], input#username, input#s-user', USER);
  await page.fill('input[name="password"], input#password, input#s-pass', PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
}

test('CollapseButton toggles each card + persists to localStorage', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);

  // Capital card: click collapse — body hides, class flips
  const cap = page.locator('.bucket-cap');
  await expect(cap).not.toHaveClass(/is-collapsed/);
  await cap.locator('.collapse-btn').first().click();
  await expect(cap).toHaveClass(/is-collapsed/);

  // Capital sub-grids hidden
  await expect(cap.locator('.dash-mini-grid')).toHaveCount(0);

  // localStorage written
  const stored = await page.evaluate(() => localStorage.getItem('ramboq.collapse.rambo.capital'));
  console.log('[capital] localStorage:', stored);
  expect(stored).toBe('1');

  // Re-load page — state restored
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);
  await expect(page.locator('.bucket-cap')).toHaveClass(/is-collapsed/);

  // Toggle back
  await page.locator('.bucket-cap .collapse-btn').first().click();
  await expect(page.locator('.bucket-cap')).not.toHaveClass(/is-collapsed/);
  const stored2 = await page.evaluate(() => localStorage.getItem('ramboq.collapse.rambo.capital'));
  expect(stored2).toBe('0');

  await page.screenshot({ path: 'test-results/collapse-check.png', fullPage: true });
});
