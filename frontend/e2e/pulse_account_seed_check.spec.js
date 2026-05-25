// Verify (a) Account picker trigger shows both accounts on cold load,
// (b) + modal Watchlist dropdown shows a Delete affordance for
// non-default lists.

import { test } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test(`pulse account seed + delete affordance [${BASE}]`, async ({ page }) => {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
    // Clear any cached account selection so this test exercises the
    // first-load seed path.
    sessionStorage.removeItem('mp.selectedAccounts');
  }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(5000);

  const slug = BASE.includes('dev') ? 'dev' : 'prod';

  // 1) Account picker trigger should show the codes (not "All accounts")
  const acctTrigger = page.locator('.mp-chrome-row > div.w-28 button.rbq-multi-trigger').first();
  const acctText = (await acctTrigger.textContent() ?? '').trim();
  console.log(`Account trigger: "${acctText}"`);

  await page.screenshot({ path: `test-results/aseed-${slug}-trigger.png`, clip: { x: 0, y: 50, width: 700, height: 150 } });

  // 2) Open + modal
  await page.getByRole('button', { name: /add symbol or watchlist/i }).click();
  await page.waitForTimeout(400);

  // Open the Watchlist dropdown
  const wlTrigger = page.locator('.search-modal [aria-label="Watchlist"] button.rbq-select-trigger, .search-modal [aria-label="Watchlist"] button').first();
  await wlTrigger.click();
  await page.waitForTimeout(300);

  // Check whether there's a non-default list to pick. If yes, pick it.
  // The dropdown options carry labels like "Default ★" / "Tech" / etc.
  const allOptions = await page.locator('li[role="option"] .rbq-select-option-label').allTextContents();
  console.log(`watchlist options: ${JSON.stringify(allOptions)}`);

  const nonDefault = allOptions.find(t => !t.includes('★') && !t.includes('New watchlist'));
  if (nonDefault) {
    console.log(`picking non-default: ${nonDefault}`);
    await page.locator('li[role="option"] .rbq-select-option-label', { hasText: new RegExp(`^${nonDefault.trim()}$`) }).first().click();
    await page.waitForTimeout(400);
    // Delete button should now be visible next to the dropdown.
    const deleteBtn = page.locator('.search-modal button', { hasText: /Delete/ });
    const visible = await deleteBtn.isVisible().catch(() => false);
    console.log(`Delete button visible for ${nonDefault}: ${visible}`);
    await page.screenshot({ path: `test-results/aseed-${slug}-modal-delete.png` });
  } else {
    console.log('no non-default watchlist to test delete affordance');
    await page.screenshot({ path: `test-results/aseed-${slug}-modal-no-nondefault.png` });
  }
});
