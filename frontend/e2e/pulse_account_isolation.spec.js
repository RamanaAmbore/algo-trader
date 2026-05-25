// Verify account filter isolates holdings: pick only ZG0790 and assert
// DH3747's TEJASNET does NOT appear in the grid.

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test(`pulse account isolation [${BASE}]`, async ({ page }) => {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
    sessionStorage.removeItem('mp.selectedAccounts');
    sessionStorage.removeItem('mp.selectedShow');
  }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(6000);

  // Baseline: TEJASNET (DH3747's holding) is visible
  const tejasBefore = await page.locator('.ag-row', { hasText: 'TEJASNET' }).count();
  console.log(`baseline TEJASNET rows: ${tejasBefore}`);

  // Open Account picker, deselect all, then pick only ZG0790
  const acctTrigger = page.locator('.mp-chrome-row > div.w-28 button.rbq-multi-trigger').first();
  await acctTrigger.click();
  await page.waitForTimeout(300);

  // Clear all first (× button)
  const clearBtn = page.locator('.mp-chrome-row > div.w-28 button.rbq-multi-clear').first();
  if (await clearBtn.isVisible().catch(() => false)) {
    await clearBtn.click();
    await page.waitForTimeout(300);
  }
  // Re-open
  await acctTrigger.click();
  await page.waitForTimeout(300);

  // Pick only ZG0790
  const zgOption = page.locator('.rbq-multi-panel .rbq-multi-option-label', { hasText: /^ZG0790$/ }).first();
  await zgOption.click();
  await page.waitForTimeout(500);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(800);

  const acctText = (await acctTrigger.textContent() ?? '').trim();
  console.log(`account picker after filter: "${acctText}"`);

  const tejasAfter = await page.locator('.ag-row', { hasText: 'TEJASNET' }).count();
  const holdRowsAfter = await page.locator('.ag-row.row-hold').count();
  console.log(`After ZG0790 filter: TEJASNET rows=${tejasAfter} total hold rows=${holdRowsAfter}`);

  // TEJASNET is DH3747-only. With only ZG0790 selected, TEJASNET should NOT appear.
  expect(tejasAfter, 'TEJASNET (DH3747 holding) should be filtered out when only ZG0790 selected').toBe(0);

  await page.screenshot({ path: 'test-results/iso-zg-only.png', fullPage: false });
});
