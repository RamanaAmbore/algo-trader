import { test, expect } from '@playwright/test';

test('direct dashboard navigation', async ({ page }) => {
  // Skip multi-viewport
  if (page.viewportSize().width < 1200) test.skip();

  // Navigate directly to dashboard
  await page.goto('https://dev.ramboq.com/dashboard');
  await page.waitForTimeout(3000);

  const url = page.url();
  console.log('Current URL:', url);

  const title = await page.title();
  console.log('Page title:', title);

  // Check if signin page is shown
  const signinCard = page.locator('text=PARTNER PORTAL');
  const isSignin = await signinCard.isVisible().catch(() => false);
  console.log('On signin page:', isSignin);

  // Take screenshot
  await page.screenshot({ path: '/tmp/quick_check.png' });
});
