// Verify NewsList renders 2 columns on wide desktop, 1 on mobile.
// Note: NewsList only mounts its <ul> when _news.length > 0, and the
// dev /api/news endpoint sometimes returns an empty feed (off-hours
// or no headlines that day). Tests soft-skip when that's the case.
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

async function clearCollapse(page) {
  await page.evaluate(() => {
    for (const k of Object.keys(localStorage)) {
      if (k.startsWith('ramboq.collapse.')) localStorage.removeItem(k);
    }
  });
}

test('desktop news shows 2-column magazine flow', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await clearCollapse(page);
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(3000);

  // Soft-skip when the upstream news feed is empty on dev.
  const ul = page.locator('.dash-row3 .newslist');
  const count = await ul.count();
  if (count === 0) {
    test.skip(true, 'news feed empty on dev — CSS path not exercised');
    return;
  }

  const colCount = await ul.evaluate(el =>
    parseInt(getComputedStyle(el).columnCount, 10) || 0
  );
  console.log('[desktop] news column-count:', colCount);
  expect(colCount).toBe(2);

  await page.screenshot({ path: 'test-results/news-2col-desktop.png', fullPage: false });
});

test('mobile news collapses to 1 column', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  await clearCollapse(page);
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(3000);

  const ul = page.locator('.dash-row3 .newslist');
  const count = await ul.count();
  if (count === 0) {
    test.skip(true, 'news feed empty on dev — CSS path not exercised');
    return;
  }

  const colCount = await ul.evaluate(el =>
    parseInt(getComputedStyle(el).columnCount, 10) || 0
  );
  console.log('[mobile] news column-count:', colCount);
  expect(colCount).toBe(1);
});
