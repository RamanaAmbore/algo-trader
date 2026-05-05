import { test, expect } from '@playwright/test';

test.describe('smoke', () => {
  test('public home renders', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveTitle(/Rambo/i);
  });

  test('algo dashboard renders or prompts sign-in', async ({ page }) => {
    const resp = await page.goto('/dashboard');
    expect(resp?.status()).toBeLessThan(500);
  });
});
