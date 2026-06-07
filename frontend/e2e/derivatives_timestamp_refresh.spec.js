// The page-header clock on /admin/derivatives (and 7 other pages) was
// previously bound via {clientTimestamp()} which captured the string at
// first render and stayed frozen. Replaced with a reactive {$nowStamp}
// store ticked by a shared 60s setInterval. Verify the displayed
// timestamp updates over time.
import { test, expect } from '@playwright/test';

test.setTimeout(120000);

const USER = process.env.PLAYWRIGHT_USER || 'rambo';
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
}

test('/admin/derivatives page-header timestamp ticks forward', async ({ page }) => {
  await signIn(page);
  await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });

  const ts = page.locator('.algo-ts').first();
  await ts.waitFor({ timeout: 15000 });
  const initial = (await ts.textContent())?.trim() || '';
  expect(initial).toMatch(/IST.*EST|IST.*EDT/);

  // Store ticks every 60s. Wait ~70s so even a worst-case minute-edge
  // start sees one tick.
  await page.waitForTimeout(70000);

  const after = (await ts.textContent())?.trim() || '';
  console.log(`[/admin/derivatives] initial="${initial}" after="${after}"`);
  expect(after).not.toBe(initial);
});
