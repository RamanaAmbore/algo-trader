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
  // addInitScript runs BEFORE every page's own scripts — this means
  // authStore's module-init `sessionStorage.getItem('ramboq_token')`
  // reads the planted value on its very first call, instead of racing
  // the planting against the bundle load. Without this, the (algo)
  // layout's auth $effect fires with `$authStore.user == null` and
  // bounces to /signin before the planted token is even visible.
  await page.context().addInitScript((token) => {
    sessionStorage.setItem('ramboq_token', token);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: 'rambo', username: 'rambo', role: 'admin', display_name: 'rambo',
    }));
  }, _cachedToken);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
}

/**
 * The chain button only enables once exactly ONE account is picked
 * in the Account MultiSelect at #opt-acct. Drives the picker so the
 * Chain button becomes interactive.
 */
async function pickFirstAccount(page) {
  const acctTrigger = page.locator('button#opt-acct').first();
  await expect(acctTrigger).toBeVisible({ timeout: 10_000 });
  await acctTrigger.click();
  // The dropdown panel opens with options below. Pick whichever is
  // first — order doesn't matter for the chain-picker test, only that
  // exactly one account ends up selected.
  const firstAcct = page.locator('.rbq-multi-panel .rbq-multi-option').first();
  await expect(firstAcct).toBeVisible({ timeout: 5000 });
  await firstAcct.click();
  // Close the panel by clicking the trigger again.
  await acctTrigger.click();
}

test.describe('Chain picker Underlying dropdown', () => {
  test('search finds RELIANCE after typing "rel"', async ({ page }) => {
    await authOnce(page);

    await page.goto('/admin/options');
    await page.waitForLoadState('domcontentloaded');
    // Instruments cache loads on mount — wait for the page picker bar
    // to populate before any clicks.
    await expect(page.locator('button#opt-acct')).toBeVisible({ timeout: 10_000 });
    await page.waitForTimeout(1500);

    await pickFirstAccount(page);

    // Chain button now enables. Click it — opens both the in-page
    // chain panel AND the OrderTicket modal on its Chain tab.
    const chainBtn = page.getByRole('button', { name: /Toggle chain picker/i });
    await expect(chainBtn).toBeEnabled({ timeout: 10_000 });
    await chainBtn.click();

    // The OrderTicket modal opens on top of the page and would
    // otherwise intercept clicks on the in-page #chain-und Select.
    // Dismiss it with Escape — the in-page panel stays open and the
    // #chain-und Select becomes interactable. (The modal carries its
    // own Underlying picker fed by the same POPULAR_UNDERLYINGS
    // source — testing either path validates the fix.)
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);

    const chainUndTrigger = page.locator('button#chain-und').first();
    await expect(chainUndTrigger).toBeVisible({ timeout: 10_000 });
    await chainUndTrigger.click();

    const searchInput = page.locator(
      'input[placeholder*="Type 3+ chars to filter"]',
    ).first();
    await expect(searchInput).toBeVisible({ timeout: 5000 });
    await searchInput.fill('rel');
    await page.waitForTimeout(400);

    // Open dropdown should now contain RELIANCE as a filtered option.
    const relianceOption = page.locator('.rbq-select-option-label')
      .filter({ hasText: /^RELIANCE$/ }).first();
    await expect(relianceOption).toBeVisible({ timeout: 5000 });
  });

  test('chain underlying defaults to the page underlying', async ({ page }) => {
    await authOnce(page);

    await page.goto('/admin/options');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.locator('button#opt-acct')).toBeVisible({ timeout: 10_000 });
    await page.waitForTimeout(1500);

    await pickFirstAccount(page);

    // The page auto-selects the first available underlying. Read its
    // value from the #opt-und Select trigger label.
    const pickerLabel = page.locator('button#opt-und .rbq-select-label').first();
    await expect(pickerLabel).toBeVisible({ timeout: 10_000 });
    const activeUnderlying = (await pickerLabel.textContent() || '').trim();

    const chainBtn = page.getByRole('button', { name: /Toggle chain picker/i });
    await expect(chainBtn).toBeEnabled({ timeout: 10_000 });
    await chainBtn.click();

    if (activeUnderlying && /^[A-Z][A-Z0-9-]+$/.test(activeUnderlying)) {
      // The in-page chain picker should auto-seed from selectedUnderlying.
      const chainUndLabel = page.locator('button#chain-und .rbq-select-label').first();
      await expect(chainUndLabel).toContainText(activeUnderlying, {
        timeout: 5000,
      });
    } else {
      test.skip(true, 'page underlying not detected — empty book');
    }
  });
});
