// Stricter check for the bug that surfaced after the unified Show
// rollout: selectedShow seeded as [] meant selectedSources = [], which
// meant showPositions / showHoldings = false, which meant positions and
// holdings never loaded, which meant availableAccounts was empty.
//
// This spec asserts: on a fresh load with no localStorage, the Show
// trigger summary is non-empty AND the grid has actual rows AND the
// Account picker (when present) lists at least one account.
//
// Run:
//   BASE_URL=https://dev.ramboq.com npx playwright test pulse_default_seed_verify.spec.js --workers=1 --project=chromium-desktop

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test(`pulse default seed surfaces sources + accounts [${BASE}]`, async ({ page }) => {
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

  const chromeRow = page.locator('.mp-chrome-row').first();
  await expect(chromeRow).toBeVisible();

  // (a) Show trigger is non-empty — the bug surfaced as the placeholder
  //     "Show…" text with no selected items. After the fix, defaults
  //     should include source tokens (and any loaded watchlists).
  const showTrigger = chromeRow.locator('> div.w-44 .rbq-multi-trigger, > div.w-44 button').first();
  await expect(showTrigger).toBeVisible();
  const triggerText = (await showTrigger.textContent() ?? '').trim();
  expect(triggerText, 'Show trigger summary not empty / not just placeholder')
    .not.toMatch(/^Show…$/);
  expect(triggerText.length, 'Show trigger summary has content').toBeGreaterThan(0);

  // (b) Open Show dropdown — its option list should include Positions
  //     and Holdings (they're always in _ALL_SOURCE_OPTIONS). Their
  //     checkboxes should be CHECKED (default seed).
  await showTrigger.click();
  await page.waitForTimeout(250);
  const panel = page.locator('.rbq-multi-panel, [role="listbox"], .rbq-multi-options').first();
  await expect(panel).toBeVisible();
  // Each option row carries text matching its label.
  const positionsRow = panel.getByText(/^positions$/i, { exact: false }).first();
  await expect(positionsRow).toBeVisible();
  const holdingsRow  = panel.getByText(/^holdings$/i,  { exact: false }).first();
  await expect(holdingsRow).toBeVisible();

  await page.screenshot({
    path: `test-results/pulse-default-seed-${BASE.includes('dev') ? 'dev' : 'prod'}.png`,
  });

  await page.keyboard.press('Escape');
  await page.waitForTimeout(200);

  // (c) Grid has at least one row — if showPositions/showHoldings were
  //     false (the bug), buildUnified would skip every row.
  //     ag-Grid renders rows with class `ag-row`.
  const rowCount = await page.locator('.ag-row').count();
  expect(rowCount, 'pulse grid has at least one row').toBeGreaterThan(0);

  // (d) Account picker lists at least one account. (The bug was the
  //     dropdown being empty because availableAccounts was [].)
  const acctTrigger = chromeRow.locator('> div.w-28 .rbq-multi-trigger, > div.w-28 button').first();
  if (await acctTrigger.isVisible().catch(() => false)) {
    await acctTrigger.click();
    await page.waitForTimeout(250);
    const acctPanel = page.locator('.rbq-multi-panel, [role="listbox"]').first();
    const acctOptions = acctPanel.locator('label, [role="option"]');
    const n = await acctOptions.count();
    expect(n, 'Account picker has at least one option').toBeGreaterThan(0);
    await page.keyboard.press('Escape');
  }
});
