// End-to-end workflow check: simulate the actual operator interaction
// path and capture screenshots so we can SEE what's on the screen.
//
// Steps:
//   1. Load /pulse, screenshot defaults.
//   2. Open Add popup (+), screenshot the popup.
//   3. Type "NIFTY" into the symbol box, screenshot typeahead.
//   4. Click first typeahead match → option picker MODAL should open.
//   5. Screenshot the option picker modal.
//   6. Cancel.

import { test } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test(`pulse full workflow + screenshots [${BASE}]`, async ({ page }) => {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  if (!tok) throw new Error(`login failed against ${BASE}`);
  await page.context().addInitScript((t) => { sessionStorage.setItem('ramboq_token', t); }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(4500);

  const slug = BASE.includes('dev') ? 'dev' : 'prod';

  // Screenshot 1 — default page
  await page.screenshot({ path: `test-results/wf-${slug}-1-default.png`, fullPage: false });
  console.log('1: default page screenshot taken');

  // Open Add popup
  await page.getByRole('button', { name: /add symbol or watchlist/i }).click();
  await page.waitForTimeout(400);
  await page.screenshot({ path: `test-results/wf-${slug}-2-add-popup.png` });
  console.log('2: Add popup open');

  // Type RELIANCE — a stock with both cash + F&O so the option chain
  // path is reachable.
  await page.locator('.search-modal input[placeholder*="Symbol"]').fill('RELIANCE');
  await page.waitForTimeout(800);
  await page.screenshot({ path: `test-results/wf-${slug}-3-typeahead.png` });
  console.log('3: typeahead visible');

  // Capture typeahead suggestions
  const suggestions = await page.locator('.search-typeahead .search-typeahead-item').allTextContents();
  console.log(`typeahead suggestions: ${JSON.stringify(suggestions.slice(0, 5))}`);

  // Click the first suggestion
  const firstSuggestion = page.locator('.search-typeahead .search-typeahead-item').first();
  if (await firstSuggestion.isVisible().catch(() => false)) {
    const sugText = (await firstSuggestion.textContent() ?? '').trim();
    console.log(`clicking first suggestion: ${sugText}`);
    await firstSuggestion.click();
    await page.waitForTimeout(1500);

    // Either Add popup closed and option picker modal opened, OR Add
    // happened directly. Look for option picker modal.
    const optPicker = page.locator('[aria-label="Pick option strike"]');
    if (await optPicker.isVisible().catch(() => false)) {
      console.log('4: option picker modal opened');
      await page.screenshot({ path: `test-results/wf-${slug}-4-option-picker.png` });

      // Capture available expiries + strikes
      const expirySelect = optPicker.locator('[aria-label="Expiry"]').first();
      const strikeSelect = optPicker.locator('[aria-label="Strike"]').first();
      console.log(`expiry trigger text: "${(await expirySelect.textContent() ?? '').trim()}"`);
      console.log(`strike trigger text: "${(await strikeSelect.textContent() ?? '').trim()}"`);

      // Cancel via Esc
      await page.keyboard.press('Escape');
      await page.waitForTimeout(400);
    } else {
      console.log('4: NO option picker modal — direct-add path taken');
    }
  } else {
    console.log('3: no typeahead suggestions visible');
  }

  await page.screenshot({ path: `test-results/wf-${slug}-5-final.png` });
});
