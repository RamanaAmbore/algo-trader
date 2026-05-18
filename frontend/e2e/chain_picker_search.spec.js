/**
 * Smoke spec: Chain-tab Underlying dropdown filter on prod.
 *
 * Verifies the chain-picker fix:
 *  1. Clicking "Chain" on /admin/options opens the OrderTicket modal
 *     on its Chain tab.
 *  2. Underlying dropdown defaults to the active underlying (auto-seeded
 *     from the page's `selectedUnderlying`), not blank or NIFTY.
 *  3. Typing 3+ characters into the searchbox filters the option list.
 *     "rel" finds RELIANCE; "infy" finds INFY; etc.
 *
 * The bug pre-fix: the curated priority list inside OptionChainTab
 * contained only indices + MCX commodities (no NSE F&O single stocks),
 * and the suggestUnderlyings('', 1000) fallback alphabetical slice cut
 * off before "R", so RELIANCE never made it into `underlyingChoices`.
 * The Select's >=3-char substring filter then matched nothing.
 *
 * Target: prod (ramboq.com)
 *   TOK=$(curl -s -X POST https://ramboq.com/api/auth/login \
 *     -H 'Content-Type: application/json' \
 *     -d '{"username":"rambo","password":"admin1234"}' \
 *     | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
 *   cd /Users/ramanambore/projects/ramboq/frontend
 *   BASE_URL=https://ramboq.com PLAYWRIGHT_AUTH_TOKEN="$TOK" \
 *   npx playwright test chain_picker_search.spec.js --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

// ── auth (shared pattern with the rest of the e2e suite) ────────────────────
const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
let _cachedToken = process.env.PLAYWRIGHT_AUTH_TOKEN || null;

async function authOnce(page) {
  if (!_cachedToken) {
    let tok = null;
    for (const delay of [0, 20_000, 65_000]) {
      if (delay) await new Promise((r) => setTimeout(r, delay));
      const resp = await page.request.post('/api/auth/login', {
        data: { username: _AUTH_USER, password: _AUTH_PASS },
      });
      if (resp.ok()) {
        tok = (await resp.json()).access_token;
        break;
      }
      if (resp.status() !== 429) {
        throw new Error(`authOnce: /api/auth/login returned ${resp.status()}`);
      }
    }
    if (!tok) {
      test.skip(true, 'rate-limited — run in isolation or pass PLAYWRIGHT_AUTH_TOKEN');
      return;
    }
    _cachedToken = tok;
  }
  await page.goto('/');
  await page.evaluate((tok) => {
    sessionStorage.setItem('ramboq_token', tok);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: 'rambo', username: 'rambo', role: 'admin', display_name: 'rambo',
    }));
  }, _cachedToken);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
}

test.describe('Chain picker Underlying dropdown', () => {
  test('search finds RELIANCE after typing "rel"', async ({ page }) => {
    await authOnce(page);

    await page.goto('/admin/options');
    await page.waitForLoadState('domcontentloaded');

    // Wait for instruments cache to be ready — the underlying dropdown
    // is empty until then. The page surfaces an `Underlying` Select
    // (#chain-und is in the in-page picker, but the OrderTicket modal
    // version is opened by the Chain button).
    await page.waitForTimeout(2000);

    // Click the Chain button on the picker bar. The button label is
    // exactly "Chain"; aria-label is "Toggle chain picker".
    const chainBtn = page.getByRole('button', { name: /Toggle chain picker/i });
    await expect(chainBtn).toBeEnabled({ timeout: 10_000 });
    await chainBtn.click();

    // The OrderTicket modal opens with its Chain tab active. Inside the
    // modal there's another Underlying Select. The MODAL Select carries
    // its own placeholder text "Type 3+ chars to filter…" — wait for it.
    const modal = page.locator('.oes-overlay, .oes-modal').first();
    await expect(modal).toBeVisible({ timeout: 5000 });

    // Find the Underlying dropdown trigger inside the modal. The
    // <Select> component renders a button-like trigger with the
    // currently-selected value as its text.
    const underlyingTrigger = modal
      .locator('.rbq-select-trigger, [role="combobox"], button')
      .filter({ hasText: /NIFTY|RELIANCE|BANKNIFTY|CRUDEOIL/i })
      .first();
    await expect(underlyingTrigger).toBeVisible({ timeout: 10_000 });

    // Click to open the dropdown panel, then type into the search input.
    await underlyingTrigger.click();
    const searchInput = page.locator(
      'input[placeholder*="Type 3+ chars to filter"]',
    ).first();
    await expect(searchInput).toBeVisible({ timeout: 5000 });

    await searchInput.fill('rel');
    await page.waitForTimeout(300);

    // After typing "rel", the filtered options should include RELIANCE.
    // Look for the option text in the open dropdown panel.
    const relianceOption = page.locator(
      '.rbq-select-option, [role="option"], li, button',
    ).filter({ hasText: /^RELIANCE$/ }).first();
    await expect(relianceOption).toBeVisible({ timeout: 5000 });
  });

  test('default underlying matches the page underlying', async ({ page }) => {
    await authOnce(page);

    await page.goto('/admin/options');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(2500);  // instruments load

    // Read the page's active underlying from the picker bar's display.
    // The selectedUnderlying Select on the picker bar carries the chosen
    // value as its trigger text.
    const pickerTrigger = page
      .locator('.rbq-select-trigger, [role="combobox"]')
      .filter({ hasText: /^[A-Z]+$/ })
      .first();
    const activeUnderlying = (await pickerTrigger.textContent() || '').trim();

    // Open the chain modal.
    const chainBtn = page.getByRole('button', { name: /Toggle chain picker/i });
    await expect(chainBtn).toBeEnabled({ timeout: 10_000 });
    await chainBtn.click();

    const modal = page.locator('.oes-overlay, .oes-modal').first();
    await expect(modal).toBeVisible({ timeout: 5000 });

    // The modal's Underlying dropdown trigger should reflect the same
    // underlying as the page's picker. The OptionChainTab seeds its
    // chainUnderlying from the `symbol` prop, which is now passed as
    // selectedUnderlying from /admin/options.
    if (activeUnderlying && /^[A-Z]+$/.test(activeUnderlying)) {
      const modalTrigger = modal
        .locator('.rbq-select-trigger, [role="combobox"]')
        .first();
      await expect(modalTrigger).toContainText(activeUnderlying, {
        timeout: 5000,
      });
    } else {
      test.skip(true, 'page underlying not detected — sim-empty book');
    }
  });
});
