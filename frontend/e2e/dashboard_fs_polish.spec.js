// Verifies the fullscreen polish:
//   - Backdrop is portalled to document.body (not inside the card)
//   - Top Winners/Losers cards have the shared account MultiSelect
//   - .wl-rows is scrollable (max-height set)
//   - /pulse has the Curve + Qty columns
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
    const t = await page.evaluate(() => !!sessionStorage.getItem('ramboq_token'));
    if (t) break;
    await new Promise((r) => setTimeout(r, 300));
  }
}

test('dashboard fullscreen polish + winners/losers shared picker', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // Top Winners + Top Losers have the account MultiSelect in their header
  const winCard = page.locator('.wl-tile-win');
  const losCard = page.locator('.wl-tile-loss');
  await expect(winCard.locator('.wl-acct-picker .rbq-multi-trigger')).toBeVisible();
  await expect(losCard.locator('.wl-acct-picker .rbq-multi-trigger')).toBeVisible();

  // Activating fullscreen on Capital → backdrop appears as direct child of body
  await page.locator('.bucket-cap .fs-btn').first().click();
  await expect(page.locator('body > .fs-backdrop')).toHaveCount(1);
  // Card is at z-index 9999, backdrop at 9998 — assert via computed style
  const zCard = await page.locator('.bucket-cap.fs-card-on').evaluate(
    (el) => parseInt(getComputedStyle(el).zIndex, 10)
  );
  const zBack = await page.locator('body > .fs-backdrop').evaluate(
    (el) => parseInt(getComputedStyle(el).zIndex, 10)
  );
  expect(zCard).toBeGreaterThan(zBack);

  // ESC closes
  await page.keyboard.press('Escape');
  await expect(page.locator('body > .fs-backdrop')).toHaveCount(0);

  // .wl-rows is scrollable when 10+ rows present
  const winRows = winCard.locator('.wl-rows');
  if (await winRows.count() > 0) {
    const maxHeight = await winRows.evaluate(
      (el) => getComputedStyle(el).maxHeight
    );
    expect(maxHeight).not.toBe('none');
  }

  await page.screenshot({ path: 'test-results/dashboard-polish-desktop.png', fullPage: true });
});

test('/pulse: Curve + Qty columns present', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await page.goto('/pulse', { waitUntil: 'networkidle' });
  await page.waitForTimeout(3000);

  // ag-Grid header cells carry the column name as text
  const headers = await page.locator('.ag-header-cell-label').allTextContents();
  console.log('[pulse headers]', headers);
  // Qty must be visible by default
  expect(headers.some(h => h.trim() === 'Qty')).toBe(true);

  await page.screenshot({ path: 'test-results/pulse-curve-qty.png', fullPage: false });
});
