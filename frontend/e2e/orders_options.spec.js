/**
 * Order placement from /admin/options — the multi-leg strategy
 * workspace.
 *
 * The chain picker (where +CE / +PE / +FUT pills land legs) is gated
 * behind "exactly one account selected" — the page disables the
 * `+` toggle until the operator narrows to a single account so every
 * leg has an unambiguous routing target.
 *
 * Test scope:
 *   1. Page mounts with the picker bar (Account / Underlying / Expiry).
 *   2. Picking a single account enables the chain-picker toggle.
 *   3. (Optional) Opening the chain picker exposes pill / info
 *      buttons. The chain takes 1-2 broker round-trips to render; we
 *      skip when no contracts surface within the timeout — it usually
 *      indicates a Kite outage or an expired session, not a UI bug.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin }  from './fixtures/auth.js';

const TIMEOUT = 30_000;

test.describe('Order placement · /admin/options', () => {

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/options');
    await expect(page.locator('#opt-und')).toBeVisible({ timeout: TIMEOUT });
    // fetchAccounts() runs after onMount; allow time so the
    // MultiSelect option list is populated before any test clicks it.
    await page.waitForTimeout(2500);
  });

  test('picker bar mounts (account / underlying / expiry)', async ({ page }) => {
    // Three Select triggers identified by stable ids.
    await expect(page.locator('#opt-acct')).toBeVisible();
    await expect(page.locator('#opt-und')).toBeVisible();
    await expect(page.locator('#opt-exp')).toBeVisible();
  });

  test('account selection enables the chain picker', async ({ page }) => {
    // Open the Account MultiSelect and pick the first concrete option.
    // MultiSelect uses `.rbq-multi-panel` (the single-pick Select
    // component uses `.rbq-select-panel`).
    await page.locator('#opt-acct').click();
    const panel = page.locator('.rbq-multi-panel').first();
    await expect(panel).toBeVisible({ timeout: TIMEOUT });
    const options = panel.locator('.rbq-multi-option');
    const count = await options.count();
    if (count < 1) {
      test.skip(true, 'no account options loaded — fetchAccounts pending or auth issue');
    }
    await options.first().click();
    // Click outside to close the multi-select panel (it doesn't auto-close).
    await page.keyboard.press('Escape');

    const chainToggle = page.locator('button.opt-add-btn-ochain').first();
    await expect(chainToggle).toBeVisible({ timeout: TIMEOUT });
    await expect(chainToggle).toBeEnabled({ timeout: 15_000 });
  });

  test('opening the chain picker exposes +/− buy/sell pills', async ({ page }) => {
    // Same pre-step — pick a single account via the MultiSelect.
    await page.locator('#opt-acct').click();
    const acctPanel = page.locator('.rbq-multi-panel').first();
    await expect(acctPanel).toBeVisible({ timeout: TIMEOUT });
    const opts = acctPanel.locator('.rbq-multi-option');
    if (await opts.count() < 1) {
      test.skip(true, 'no accounts loaded');
    }
    await opts.first().click();
    await page.keyboard.press('Escape');

    const chainToggle = page.locator('button.opt-add-btn-ochain').first();
    await expect(chainToggle).toBeEnabled({ timeout: 15_000 });
    await chainToggle.click();

    // Chain picker mounts. The contracts render asynchronously after
    // a broker fetch; wait up to TIMEOUT for the first pill to appear.
    // If none appear (Kite outage, illiquid underlying), skip.
    try {
      await page.locator('button.chain-btn').first().waitFor({ state: 'visible', timeout: TIMEOUT });
    } catch (_) {
      test.skip(true, 'chain picker rendered but no contracts loaded (Kite outage / illiquid underlying)');
    }
    const buyPills = page.locator('button.chain-btn.chain-btn-buy');
    expect(await buyPills.count(), 'no +BUY pills in chain picker').toBeGreaterThan(0);
  });
});
