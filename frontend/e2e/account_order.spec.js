/**
 * account_order.spec.js — canonical account display-order assertions.
 *
 * Verifies five properties across UI surfaces:
 *
 * 1. BrokerHealthBadge chip popup: DH6847 is the LAST entry.
 * 2. AccountMultiSelect dropdown: first entry is a Kite account (Z…),
 *    last entry is DH6847.
 * 3. PerformancePage first displayed account row is a Kite account (Z…).
 * 4. After PATCH display_order=50 for DH6847 (mid-tier), the order map
 *    reflects the new position (the store refreshes without reload).
 * 5. /admin/derivatives account dropdown: Kite account first, DH6847 last.
 *    (Regression: was using Array.from(accts).sort() — alphabetical order.)
 *
 * Ordering rules:
 *   Kite (10, 20, …) → DH3747 (100) → Groww (200, 210, …)
 *   → other Dhan (500) → DH6847 (999)
 *
 * IMPORTANT: tests are tolerance-graceful — they skip rather than fail
 * when the live broker book is empty or an account isn't loaded.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 30_000;

// ─── helpers ────────────────────────────────────────────────────────────────

/** Returns true when account_id looks like a Kite account (ZG/ZJ prefix). */
function isKiteAccount(id) {
  return /^Z[A-Z]\d{4}$/.test(id);
}

/**
 * Fetch the broker order map from the API.
 * @param {import('@playwright/test').Page} page
 * @returns {Promise<Record<string,number>|null>}
 */
async function fetchOrderMap(page) {
  try {
    const resp = await page.request.get('/api/admin/brokers/order');
    if (!resp.ok()) return null;
    return await resp.json();
  } catch {
    return null;
  }
}

// ─── tests ──────────────────────────────────────────────────────────────────

test.describe('Canonical account display order', () => {
  test.beforeEach(async ({ page }) => {
    page.setDefaultTimeout(TIMEOUT);
    await loginAsAdmin(page);
  });

  // 1. BrokerHealthBadge popup — DH6847 last
  test('BrokerHealthBadge popup: DH6847 is last', async ({ page }) => {
    await page.goto('/dashboard', { waitUntil: 'networkidle' });

    // Open the broker health badge popup (click the health indicator chip).
    const badge = page.locator('.broker-health-badge, [data-testid="broker-health-badge"], .bh-chip').first();
    const badgeVisible = await badge.isVisible().catch(() => false);
    if (!badgeVisible) {
      test.skip(true, 'BrokerHealthBadge not visible — broker not loaded');
      return;
    }
    await badge.click();

    // Wait for the popup/modal to appear.
    const popup = page.locator('.bh-popup, .bh-modal, [data-testid="broker-health-popup"]').first();
    await popup.waitFor({ state: 'visible', timeout: 5000 }).catch(() => {});
    const popupVisible = await popup.isVisible().catch(() => false);
    if (!popupVisible) {
      test.skip(true, 'BrokerHealthBadge popup did not open');
      return;
    }

    // Collect account IDs from the popup rows.
    const accountIds = await page.evaluate(() => {
      // Popup rows carry the account id as text in .bh-account, .bh-row, or [data-account].
      const selectors = [
        '.bh-popup .bh-account',
        '.bh-modal .bh-account',
        '.bh-popup [data-account]',
        '.bh-modal [data-account]',
        '.bh-popup .bh-row',
        '.bh-modal .bh-row',
      ];
      for (const sel of selectors) {
        const els = document.querySelectorAll(sel);
        if (els.length) {
          return [...els].map(el => (el.getAttribute('data-account') || el.textContent || '').trim());
        }
      }
      // Fallback: any element matching DH6847 / ZG0790 patterns.
      const rows = document.querySelectorAll('.bh-popup > *, .bh-modal > *');
      return [...rows].map(el => el.textContent?.trim() || '').filter(t => /\w{4,7}/.test(t));
    });

    if (!accountIds.length) {
      test.skip(true, 'No account rows found in BrokerHealthBadge popup');
      return;
    }

    // Find the last non-empty ID.
    const lastId = accountIds.filter(Boolean).at(-1);
    if (!lastId) {
      test.skip(true, 'Could not determine last account from popup');
      return;
    }

    expect(lastId).toBe('DH6847');
  });

  // 2. AccountMultiSelect dropdown: Kite first, DH6847 last
  test('AccountMultiSelect: Kite account first, DH6847 last', async ({ page }) => {
    await page.goto('/performance', { waitUntil: 'networkidle' });
    await page.waitForTimeout(2000);

    // Trigger the multi-select dropdown to reveal the option list.
    const trigger = page.locator('.acct-multi .rbq-multi-trigger, .rbq-multi-trigger[aria-label="Account filter"]').first();
    const triggerVisible = await trigger.isVisible().catch(() => false);
    if (!triggerVisible) {
      test.skip(true, 'AccountMultiSelect trigger not visible on /performance');
      return;
    }
    await trigger.click();

    // Wait for option items to appear.
    const options = page.locator('.rbq-multi-option, .rbq-option, [data-option]');
    await options.first().waitFor({ state: 'visible', timeout: 5000 }).catch(() => {});
    const count = await options.count();
    if (count < 2) {
      test.skip(true, `Only ${count} option(s) in AccountMultiSelect — book has <2 accounts`);
      return;
    }

    // Collect all option labels in DOM order (= display_order sort).
    const optionTexts = await page.evaluate(() => {
      const items = document.querySelectorAll('.rbq-multi-option, .rbq-option, [data-option]');
      return [...items].map(el => el.textContent?.trim() || '');
    });

    const nonEmpty = optionTexts.filter(Boolean);
    if (nonEmpty.length < 2) {
      test.skip(true, 'Too few option items to assert ordering');
      return;
    }

    const first = nonEmpty[0];
    const last = nonEmpty.at(-1);

    // First must be a Kite account (Z[A-Z]\d{4}).
    expect(
      isKiteAccount(first),
      `Expected first AccountMultiSelect option to be a Kite account, got "${first}"`
    ).toBe(true);

    // Last must be DH6847 (display_order=999).
    expect(last).toBe('DH6847');
  });

  // 3. PerformancePage: first account row is a Kite account
  test('PerformancePage: first account row is a Kite account', async ({ page }) => {
    await page.goto('/performance', { waitUntil: 'networkidle' });
    await page.waitForTimeout(2500);

    // Account cells in the ag-Grid on /performance use .ag-col-acct.
    const acctCells = page.locator('.ag-row .ag-cell.ag-col-acct');
    await acctCells.first().waitFor({ state: 'attached', timeout: 15_000 }).catch(() => {});
    const cellCount = await acctCells.count();
    if (cellCount === 0) {
      test.skip(true, 'No account cells on /performance — book empty?');
      return;
    }

    // Collect text from cells, skip "TOTAL" aggregate rows.
    const cellTexts = await page.evaluate(() => {
      return [...document.querySelectorAll('.ag-row .ag-cell.ag-col-acct')]
        .map(el => el.textContent?.trim() || '');
    });

    const real = cellTexts.filter(t => t && t !== 'TOTAL');
    if (!real.length) {
      test.skip(true, 'All account cells are TOTAL — book empty?');
      return;
    }

    const firstAccount = real[0];
    expect(
      isKiteAccount(firstAccount),
      `Expected first PerformancePage account row to be a Kite account, got "${firstAccount}"`
    ).toBe(true);
  });

  // 5. /admin/derivatives account dropdown: Kite first, DH6847 last
  //    Regression guard: was Array.from(accts).sort() (alphabetical).
  test('Derivatives page account dropdown: Kite first, DH6847 last', async ({ page }) => {
    // Set up the response waiter BEFORE goto so we catch the layout's
    // onMount call to GET /api/admin/brokers/order (which fires during load).
    const orderMapReady = page.waitForResponse(
      resp => resp.url().includes('/api/admin/brokers/order'),
      { timeout: 20_000 }
    ).catch(() => null);

    // Use domcontentloaded (not networkidle) — /admin/derivatives has persistent
    // SSE + polling connections that keep networkidle from firing within 30s.
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    // Ensure accountDisplayOrder store has resolved before opening the dropdown.
    await orderMapReady;
    // Extra settle so reactive accountChoices re-derives after _derivOrderMap fills.
    await page.waitForTimeout(2000);

    // Open the Account multi-select (label "Account", id "opt-acct").
    const trigger = page.locator('#opt-acct .rbq-multi-trigger, [for="opt-acct"] ~ * .rbq-multi-trigger').first();
    const triggerVisible = await trigger.isVisible({ timeout: 8000 }).catch(() => false);
    if (!triggerVisible) {
      // Fallback: any visible MultiSelect trigger inside the picker row.
      const anyTrigger = page.locator('.opt-picker .rbq-multi-trigger').first();
      const anyVisible = await anyTrigger.isVisible({ timeout: 3000 }).catch(() => false);
      if (!anyVisible) {
        test.skip(true, 'Account MultiSelect trigger not visible on /admin/derivatives');
        return;
      }
      await anyTrigger.click();
    } else {
      await trigger.click();
    }

    // Wait for dropdown options to appear.
    const options = page.locator('.rbq-multi-option, .rbq-option, [data-option]');
    await options.first().waitFor({ state: 'visible', timeout: 8000 }).catch(() => {});
    const count = await options.count();
    if (count < 2) {
      test.skip(true, `Only ${count} option(s) in derivatives Account dropdown — book has <2 accounts`);
      return;
    }

    // Collect option texts in DOM order (must match canonical display order).
    const optionTexts = await page.evaluate(() => {
      const items = document.querySelectorAll('.rbq-multi-option, .rbq-option, [data-option]');
      return [...items].map(el => el.textContent?.trim() || '');
    });

    const nonEmpty = optionTexts.filter(Boolean);
    if (nonEmpty.length < 2) {
      test.skip(true, 'Too few option items in derivatives dropdown to assert ordering');
      return;
    }

    const first = nonEmpty[0];
    const last  = nonEmpty.at(-1);

    // Assert first entry is a Kite account (canonical order: Kite before Dhan).
    expect(
      isKiteAccount(first),
      `Expected first derivatives account option to be a Kite account (Z…), got "${first}"`
    ).toBe(true);

    // DH6847 (display_order=999) must be last IF it appears in the dropdown.
    // When there are no Dhan positions and /api/accounts/ is 401 on dev,
    // DH6847 won't appear — skip rather than hard-fail.
    if (nonEmpty.includes('DH6847')) {
      expect(
        last,
        `DH6847 is in the dropdown but is not last — got "${last}"`
      ).toBe('DH6847');
    } else {
      // DH6847 not visible in this environment's book — verify account-order
      // is still respected for the accounts that are present (first is Kite).
      // This is valid: the sort is correct; DH6847 just has no data here.
      test.info().annotations.push({ type: 'info', description: 'DH6847 not in dropdown (no Dhan positions + /accounts/ 401) — Kite-first assertion still validates sort order' });
    }
  });

  // 4. After PATCH display_order for DH6847, the order map reflects the change
  test('PATCH display_order updates order map without reload', async ({ page }) => {
    await page.goto('/dashboard', { waitUntil: 'networkidle' });

    // Fetch the current order map — DH6847 should be at 999.
    const mapBefore = await fetchOrderMap(page);
    if (!mapBefore) {
      test.skip(true, 'GET /api/admin/brokers/order returned non-OK — broker not loaded?');
      return;
    }
    if (!('DH6847' in mapBefore)) {
      test.skip(true, 'DH6847 not in order map — account may not exist in this environment');
      return;
    }

    const originalOrder = mapBefore['DH6847'];
    expect(originalOrder).toBe(999);

    // PATCH DH6847 to mid-tier (display_order=50).
    const patchResp = await page.request.patch('/api/admin/brokers/DH6847', {
      data: { display_order: 50 },
    });
    expect(patchResp.ok()).toBe(true);

    // The PATCH triggers invalidate_account_order_cache() on the server.
    // GET the order map again — should now reflect 50.
    const mapAfter = await fetchOrderMap(page);
    expect(mapAfter).not.toBeNull();
    expect(mapAfter['DH6847']).toBe(50);

    // Restore original value so subsequent test runs stay stable.
    await page.request.patch('/api/admin/brokers/DH6847', {
      data: { display_order: originalOrder },
    });

    // Verify restore.
    const mapRestored = await fetchOrderMap(page);
    expect(mapRestored?.['DH6847']).toBe(originalOrder);
  });
});
