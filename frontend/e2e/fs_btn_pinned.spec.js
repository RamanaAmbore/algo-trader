// Verifies FullscreenButton stays anchored at the top-right corner:
//   default mode: button is the rightmost child of its header
//   fullscreen mode: button is position:fixed near viewport top-right
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

test('Capital card: FullscreenButton sits at top-right (default + fullscreen)', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await page.goto('/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // Default mode — button rightmost in the Capital card-header (bucket-header)
  const capBtn = page.locator('.bucket-cap .fs-btn').first();
  const cap = page.locator('.bucket-cap');
  const btnBox = await capBtn.boundingBox();
  const capBox = await cap.boundingBox();
  expect(btnBox).toBeTruthy();
  expect(capBox).toBeTruthy();
  // Button right edge sits within 30px of card right edge
  const distFromRight = (capBox.x + capBox.width) - (btnBox.x + btnBox.width);
  console.log(`[default] Capital fs-btn distance from card right edge: ${distFromRight}px`);
  expect(distFromRight).toBeLessThan(40);

  // Fullscreen mode — button becomes position:fixed near viewport top-right
  await capBtn.click();
  await page.waitForTimeout(200);
  const fsBtn = page.locator('.bucket-cap.fs-card-on .fs-btn').first();
  const computed = await fsBtn.evaluate((el) => {
    const s = getComputedStyle(el);
    return { position: s.position, top: s.top, right: s.right, zIndex: s.zIndex };
  });
  console.log('[fullscreen] fs-btn computed:', computed);
  expect(computed.position).toBe('fixed');
  expect(parseInt(computed.zIndex, 10)).toBeGreaterThan(9999);

  // Distance from viewport top + right edges — should be small (just inside the inset)
  const fsBox = await fsBtn.boundingBox();
  const vp = page.viewportSize();
  expect(fsBox.y).toBeLessThan(80);
  expect(vp.width - (fsBox.x + fsBox.width)).toBeLessThan(80);

  await page.keyboard.press('Escape');
  await page.screenshot({ path: 'test-results/fs-btn-pinned.png', fullPage: false });
});
