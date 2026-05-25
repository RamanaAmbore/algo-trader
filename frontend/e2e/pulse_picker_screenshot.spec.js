import { test, expect } from '@playwright/test';

test('pulse layout screenshot', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"], input#username, input#s-user', 'rambo');
  await page.fill('input[name="password"], input#password, input#s-pass', process.env.PLAYWRIGHT_PASS || 'admin1234');
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
  await page.goto('/pulse', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);
  await page.screenshot({ path: 'test-results/pulse-with-multiselect.png', fullPage: false });
});
