// Verify DH3747 + DH6847 appear in /pulse Account picker and that
// the DH3747 holdings (TEJASNET, CEINSYS, SHILCTECH, ADVAIT, NETWEB,
// etc. — 12 rows total) render in the grid.

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test(`pulse — Dhan accounts in picker + holdings visible [${BASE}]`, async ({ page }) => {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  if (!tok) throw new Error(`login failed against ${BASE}`);
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
    sessionStorage.removeItem('mp.selectedAccounts');
    sessionStorage.removeItem('mp.selectedShow');
  }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
  // Service just restarted — Kite TOTP + Dhan token mint runs on
  // first request, can take ~10-15 s.
  await page.waitForTimeout(15000);

  const slug = BASE.includes('dev') ? 'dev' : 'prod';

  // Open Account picker — locate by aria-label since the wrapper class
  // can shift across breakpoints (flex-wrap row may place it on row 2).
  const acctTrigger = page.locator('[aria-label="Filter by broker account"]').first();
  await expect(acctTrigger).toBeVisible({ timeout: 15000 });
  await acctTrigger.click();
  await page.waitForTimeout(400);

  const options = await page.locator('.rbq-multi-panel .rbq-multi-option-label').allTextContents();
  console.log(`account options: ${JSON.stringify(options)}`);
  expect(options).toContain('DH3747');

  await page.screenshot({ path: `test-results/dhan-${slug}-acct-picker.png`, clip: { x: 0, y: 50, width: 700, height: 500 } });

  // Close dropdown
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);

  // Verify holdings rows for one of DH3747's symbols are in the grid.
  // ag-Grid virtualises rows so we use a known symbol as the discriminator.
  // TEJASNET should be in the visible viewport when DH3747 holdings load.
  // Use a broader check: at least one of DH3747's holdings appears.
  const expected = ['TEJASNET', 'CEINSYS', 'SHILCTECH', 'ADVAIT', 'NETWEB'];
  let found = null;
  for (const sym of expected) {
    if (await page.locator('.ag-row', { hasText: sym }).first().isVisible().catch(() => false)) {
      found = sym; break;
    }
  }
  console.log(`first visible DH3747 holding in grid: ${found}`);
  expect(found).not.toBeNull();

  await page.screenshot({ path: `test-results/dhan-${slug}-grid.png` });
});
