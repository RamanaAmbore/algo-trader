// Verify the /pulse "+ unified Add popup" refactor + the always-visible
// Curve column. Targets dev.ramboq.com first; same spec runs against
// ramboq.com (prod) by toggling BASE_URL env var.
//
// What this verifies:
//   (a) Chrome row has exactly ONE add button at the END — the legacy
//       🔍 / + List buttons are gone.
//   (b) The chrome row is left-aligned (no mp-chrome-spacer pushing the
//       chrome cluster right).
//   (c) Clicking + opens ONE overlay (.search-overlay) that contains BOTH
//       sections: "Add symbol" + "New watchlist".
//   (d) The grid header carries a "Curve" column.
//
// Run:
//   BASE_URL=https://dev.ramboq.com PLAYWRIGHT_PASS=... npx playwright test pulse_unified_add_verify.spec.js --workers=1
//   BASE_URL=https://ramboq.com    PLAYWRIGHT_PASS=... npx playwright test pulse_unified_add_verify.spec.js --workers=1

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test(`pulse unified + popup + Curve column [${BASE}]`, async ({ page }) => {
  // Auth — try ambore (admin) then rambo (fallback).
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
  await page.waitForTimeout(3500);

  // (a) Legacy 🔍 (search-and-add) button is gone — its aria-label was
  //     "Search and add symbol".
  await expect(page.getByRole('button', { name: /search.*add symbol/i })).toHaveCount(0);
  // (a) Legacy "+ List" button is gone — its aria-label was "New watchlist".
  await expect(page.getByRole('button', { name: /new watchlist/i })).toHaveCount(0);
  // (a) New unified + button exists.
  const addBtn = page.getByRole('button', { name: /add symbol or watchlist/i });
  await expect(addBtn).toBeVisible();
  await expect(addBtn).toHaveText('+');

  // (b) `+` button is the LAST element in the chrome row — get its
  //     parent's children and verify position. mp-chrome-row > children:
  //     watchlist tabs, account picker, sources, then + button.
  const chromeRow = page.locator('.mp-chrome-row').first();
  await expect(chromeRow).toBeVisible();
  const lastChild = chromeRow.locator('> *').last();
  // Last element should be the + button (or its wrapper containing it).
  const lastIsAdd = await lastChild.evaluate((el) =>
    el.classList.contains('mp-add-btn') ||
    el.querySelector('.mp-add-btn') !== null,
  );
  expect(lastIsAdd, '+ button is last child of chrome row').toBe(true);

  // (b) mp-chrome-spacer no longer exists in the markup.
  await expect(page.locator('.mp-chrome-spacer')).toHaveCount(0);

  await page.screenshot({
    path: `test-results/pulse-chrome-row-${BASE.includes('dev') ? 'dev' : 'prod'}.png`,
    clip: { x: 0, y: 60, width: 1440, height: 120 },
  });

  // (c) Click + → unified popup opens with BOTH sections.
  await addBtn.click();
  await page.waitForTimeout(300);
  const overlay = page.locator('.search-overlay');
  await expect(overlay).toBeVisible();
  // Single modal — title is "Add to Market Pulse".
  await expect(page.locator('.search-title')).toHaveText('Add to Market Pulse');
  // Two section labels — "Watchlist" target picker + "Add symbol"
  // typeahead. (The legacy "New watchlist" section was folded into the
  // Watchlist dropdown's "+ New watchlist" option; the inline name
  // input only renders when that's selected.)
  const sectionLabels = page.locator('.mp-add-section-label');
  await expect(sectionLabels).toHaveCount(2);
  await expect(sectionLabels.nth(0)).toHaveText(/watchlist/i);
  await expect(sectionLabels.nth(1)).toHaveText(/add symbol/i);
  // Single Add button lives in the symbol row.
  await expect(page.locator('.search-modal').getByRole('button', { name: /^Add$/ })).toBeVisible();

  await page.screenshot({
    path: `test-results/pulse-add-popup-${BASE.includes('dev') ? 'dev' : 'prod'}.png`,
  });

  // Close popup.
  await page.keyboard.press('Escape');
  await page.waitForTimeout(200);
  await expect(overlay).toHaveCount(0);

  // (d) Curve column header is visible in the grid. ag-Grid renders
  //     header text in .ag-header-cell-text spans.
  const curveHeader = page.locator('.ag-header-cell-text', { hasText: /^Curve$/ });
  await expect(curveHeader).toBeVisible();

  await page.screenshot({
    path: `test-results/pulse-grid-header-${BASE.includes('dev') ? 'dev' : 'prod'}.png`,
    clip: { x: 0, y: 100, width: 1440, height: 220 },
  });
});
