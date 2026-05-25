// Verify the Source and Account filters on /pulse actually scope
// the grid rows. ag-Grid virtualises rows (DOM rows = viewport size,
// not data); per-class .ag-row.pos-* counts are data-driven.

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
const POS_SELECTOR = '.ag-row.pos-long, .ag-row.pos-short, .ag-row.row-pos';

async function login(page) {
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
}

test(`pulse Source filter actually filters [${BASE}]`, async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(4500);

  const posBefore = await page.locator(POS_SELECTOR).count();
  test.skip(posBefore === 0, 'no positions in book');

  const showTrigger = page.locator('.mp-chrome-row > div.w-44 button').first();
  await showTrigger.click();
  await page.waitForTimeout(300);
  await page.locator('.rbq-multi-option-label', { hasText: /^Positions$/ }).first().click();
  await page.waitForTimeout(800);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);

  const posAfter = await page.locator(POS_SELECTOR).count();
  expect(posAfter, `Position rows go to 0 (was ${posBefore})`).toBe(0);
});

test(`pulse Account filter actually filters [${BASE}]`, async ({ page }) => {
  await login(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(4500);

  const posBefore = await page.locator(POS_SELECTOR).count();
  test.skip(posBefore === 0, 'no positions in book');

  const acctWrapper = page.locator('.mp-chrome-row > div.w-28').first();
  await expect(acctWrapper).toBeVisible();
  const acctTrigger = acctWrapper.locator('button').first();

  // Open dropdown, read accounts.
  await acctTrigger.click();
  await page.waitForTimeout(300);
  const allLabels = await page.locator('.rbq-multi-panel .rbq-multi-option-label').allTextContents();
  console.log(`accounts: ${JSON.stringify(allLabels)}`);
  test.skip(allLabels.length < 2, 'only one account in dataset');

  // Pick the LAST account in the list — if all positions are in the
  // first account (common: one master account), picking the OTHER
  // account should give 0 (filter works) or stay at posBefore (filter
  // broken).
  const acctLabel = allLabels[allLabels.length - 1];
  const opt = page.locator('.rbq-multi-panel .rbq-multi-option-label', {
    hasText: new RegExp(`^${acctLabel}$`),
  }).first();
  await opt.click({ timeout: 5000 });
  await page.waitForTimeout(1200);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(500);
  const scopedPos = await page.locator(POS_SELECTOR).count();
  console.log(`scope=${acctLabel} pos=${scopedPos} (vs unfiltered ${posBefore})`);
  // If posBefore === scopedPos AND there are 2+ accounts, the filter is
  // broken (one account can't carry all positions while another also
  // does). Strictly: scopedPos must be less than posBefore.
  expect(scopedPos,
    `picking ${acctLabel} must reduce position rows below ${posBefore}`)
    .toBeLessThan(posBefore);
});
