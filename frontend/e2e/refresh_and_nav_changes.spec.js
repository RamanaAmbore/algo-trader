// Verifies the bundle of UI changes:
//   • .algo-ts timestamp colour is sky-300 (#7dd3fc) — distinct from
//     the amber page-title chip.
//   • /automation header no longer carries a History pill (Activity
//     lives on /activity?tab=agent); /admin/alerts is reached via nav.
//   • /pulse toolbar carries a refresh icon button.
//   • /pulse sparkline column header reads "5d" (renamed from "Curve").
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

test('UI bundle: ts colour + automation header + pulse refresh + 5d header', async ({ page }) => {
  await signIn(page);

  // /automation — the History pill was removed; the timestamp computes to sky-300.
  await page.goto('/automation', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('a.history-pill')).toHaveCount(0);

  const ts = page.locator('.algo-ts').first();
  await expect(ts).toBeVisible();
  const tsColor = await ts.evaluate((el) => getComputedStyle(el).color);
  console.log(`[/automation] .algo-ts color = ${tsColor}`);
  expect(tsColor).toBe('rgb(125, 211, 252)');

  // /pulse — toolbar refresh icon, and the 5d column header.
  await page.goto('/pulse', { waitUntil: 'networkidle' });
  await page.locator('.ag-row').first().waitFor({ timeout: 20000 });

  const refresh = page.locator('.rf-btn').first();
  await expect(refresh).toBeVisible();
  const refreshTitle = await refresh.getAttribute('title');
  console.log(`[/pulse] refresh button title = "${refreshTitle}"`);
  expect(refreshTitle).toMatch(/Refresh/);

  const fiveDHeader = page.locator('.ag-header-cell-text', { hasText: '5d' }).first();
  await expect(fiveDHeader).toBeVisible({ timeout: 10000 });

  // 5d column width pinned at 44 px (SVG 32 + 6 px padding each side).
  await page.setViewportSize({ width: 360, height: 800 });
  await page.waitForTimeout(800);
  const sparkCol = page.locator('[col-id="sparkline"]').first();
  const sparkBox = await sparkCol.boundingBox();
  console.log(`[/pulse mobile] sparkline col width = ${sparkBox?.width}`);
  expect(sparkBox?.width).toBe(44);

  // LTP column pinned at 77 px so five-digit instrument prices have
  // breathing room.
  const ltpCol = page.locator('[col-id="ltp"]').first();
  const ltpBox = await ltpCol.boundingBox();
  console.log(`[/pulse mobile] LTP col width = ${ltpBox?.width}`);
  expect(ltpBox?.width).toBe(77);
});
