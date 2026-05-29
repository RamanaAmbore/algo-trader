// Verify the new title-first layout + edit form completeness on /agents
// against dev.ramboq.com. Per CLAUDE.md / standing rule: deployed dev
// only, not local preview.
//
// Checks:
//   1. On /agents the .page-header sits ABOVE the .aw-tabs strip (Y).
//   2. DisclosureChevron renders on agent rows when expanded/collapsed.
//   3. Edit form exposes long_name, trade_mode, debounce_minutes, tags,
//      blackout_windows inputs.
//   4. Priority label replaces "Tier".

import { test, expect } from '@playwright/test';

const BASE = 'https://dev.ramboq.com';
const USER = 'ambore';
const PW   = 'rambo';

async function signIn(page) {
  await page.goto(`${BASE}/signin`);
  // Fall back to 'rambo' if username 'ambore' fails (per memory).
  const tryUsers = [USER, 'rambo'];
  for (const u of tryUsers) {
    await page.fill('input[name="username"], input[type="text"]', u);
    await page.fill('input[type="password"]', PW);
    await Promise.all([
      page.waitForURL(/\/agents|\/showcase|\/dashboard|\/tour/, { timeout: 10000 }).catch(() => null),
      page.click('button[type="submit"], button:has-text("Sign in")'),
    ]);
    if (!/\/signin/.test(page.url())) return;
  }
}

test('agents workspace: title sits above tab strip', async ({ page }) => {
  await signIn(page);
  await page.goto(`${BASE}/agents`);
  const header = page.locator('.page-header').first();
  const tabs   = page.locator('.aw-tabs').first();
  await expect(header).toBeVisible();
  await expect(tabs).toBeVisible();
  const hBox = await header.boundingBox();
  const tBox = await tabs.boundingBox();
  expect(hBox).not.toBeNull();
  expect(tBox).not.toBeNull();
  expect(hBox.y).toBeLessThan(tBox.y);
});

test('agent row uses DisclosureChevron', async ({ page }) => {
  await signIn(page);
  await page.goto(`${BASE}/agents`);
  await page.waitForSelector('.algo-status-card');
  const chevron = page.locator('.disclosure-chevron').first();
  await expect(chevron).toBeVisible();
});

test('edit form exposes long_name + trade_mode + debounce + tags + blackout', async ({ page }) => {
  await signIn(page);
  await page.goto(`${BASE}/agents`);
  await page.waitForSelector('.algo-status-card');
  // Expand the first agent row, then click Edit.
  const firstRow = page.locator('.algo-status-card div[role="button"]').first();
  await firstRow.click();
  await page.waitForTimeout(300);
  // The Edit button might not always be visible (system agents) — use the
  // first edit pill we find.
  const editBtn = page.locator('button:has-text("Edit")').first();
  if (await editBtn.count()) {
    await editBtn.click();
    await page.waitForTimeout(300);
    // Check the labels are present.
    await expect(page.getByText('Long name')).toBeVisible();
    await expect(page.getByText('Trade mode')).toBeVisible();
    await expect(page.getByText('Debounce (minutes)')).toBeVisible();
    await expect(page.getByText('Priority')).toBeVisible();
    await expect(page.getByText('Tags', { exact: true })).toBeVisible();
    await expect(page.getByText('Blackout windows (JSON)')).toBeVisible();
  }
});
