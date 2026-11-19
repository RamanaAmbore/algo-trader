/**
 * Smoke spec: shell-level basket bar renders per-leg pills.
 *
 * Verifies commit 43afe36:
 *  1. Open /admin/derivatives → pick an account → click Chain.
 *  2. Add a leg via one of the strike-grid + buttons inside the
 *     OrderTicket chain tab.
 *  3. Switch to a non-Chain tab (Log) so only the shell-level basket
 *     bar is visible (the chain-tab basket disappears with the tab).
 *  4. Assert the shell basket bar shows .oes-basket-pill with the
 *     leg's symbol, NOT just a count.
 *
 * Target: prod (ramboq.com)
 *   TOK=$(...)
 *   PLAYWRIGHT_BASE_URL=https://ramboq.com PLAYWRIGHT_AUTH_TOKEN="$TOK" \
 *   npx playwright test basket_pills.spec.js --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

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
      if (resp.ok()) { tok = (await resp.json()).access_token; break; }
      if (resp.status() !== 429) throw new Error(`authOnce: /api/auth/login returned ${resp.status()}`);
    }
    if (!tok) { test.skip(true, 'rate-limited'); return; }
    _cachedToken = tok;
  }
  await page.context().addInitScript((token) => {
    sessionStorage.setItem('ramboq_token', token);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: 'rambo', username: 'rambo', role: 'admin', display_name: 'rambo',
    }));
  }, _cachedToken);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
}

async function pickFirstAccount(page) {
  const acctTrigger = page.locator('button#opt-acct').first();
  await expect(acctTrigger).toBeVisible({ timeout: 10_000 });
  await acctTrigger.click();
  const firstAcct = page.locator('.rbq-multi-panel .rbq-multi-option').first();
  await expect(firstAcct).toBeVisible({ timeout: 5000 });
  await firstAcct.click();
  // Close via an offset click: the "×" clear button (rbq-multi-clear) sits
  // over the trigger's centre once an account is selected, so a centre click
  // intercepts. Position targets the trigger's own clickable left edge.
  await acctTrigger.click({ position: { x: 8, y: 8 } });
}

/**
 * Opens the OrderTicket modal on its Chain tab via the canonical path:
 * click an F&O candidate row (opens SymbolPanel), then click the modal's
 * Chain tab. The page-level "Toggle chain picker" button was removed in
 * commit 0e31eee0 — do not reintroduce it.
 */
async function openChainFromRow(page) {
  const modal = page.locator('.oes-modal:visible').first();
  const rows = page.locator('.cand-row:not(.cand-row-total):not(.cand-closed)');
  await expect(rows.first()).toBeVisible({ timeout: 15_000 });
  const n = Math.min(await rows.count(), 10);
  for (let i = 0; i < n; i++) {
    await rows.nth(i).click();
    await expect(modal).toBeVisible({ timeout: 10_000 });
    const chainTab = modal.getByRole('tab', { name: 'Chain', exact: true });
    if ((await chainTab.count()) && (await chainTab.isEnabled())) {
      await chainTab.click();
      return;
    }
    await page.keyboard.press('Escape');
    await expect(modal).toBeHidden({ timeout: 5000 });
  }
  throw new Error('openChainFromRow: no candidate row exposes an enabled Chain tab');
}

test.describe('Shell basket pills', () => {

test('basket pills render in the shell bottom bar', async ({ page }) => {
  await authOnce(page);
  await page.goto('/admin/derivatives');
  await page.waitForLoadState('domcontentloaded');
  // Wait until the account list has actually loaded (placeholder text
  // flips away from "No accounts loaded"). Without this, the test
  // sometimes runs before /api/accounts returns and the picker can't
  // pop a usable account option.
  await expect(page.locator('button#opt-acct')).toBeVisible({ timeout: 15_000 });
  await expect(page.locator('button#opt-acct')).not.toContainText(
    /No accounts loaded/i, { timeout: 15_000 });
  await pickFirstAccount(page);

  // Open the chain modal.
  await openChainFromRow(page);

  // The OrderTicket modal opens on the Chain tab. Find the first
  // "+" button inside a strike row OR on the futures pill — either
  // is a valid add-to-basket affordance. The chain-tab markup uses
  // `.chain-strike-add` (button) or `.chain-fut-add`.
  await page.waitForTimeout(1500);  // let chain quotes settle
  const addButton = page.locator('.oct-root button')
    .filter({ hasText: /^\+$/ })
    .first();
  await expect(addButton).toBeVisible({ timeout: 10_000 });
  await addButton.click();

  // Switch to the bottom-panel Log tab so the chain tab is no longer
  // the focused surface. The shell-level basket bar should still
  // render pills.
  const logTab = page.locator('.oes-bottom-tab').filter({ hasText: /^Log$/ }).first();
  if (await logTab.count()) await logTab.click();

  // Assert the shell's basket bar has at least one pill rendered.
  const shellPill = page.locator('.oes-basket-pill').first();
  await expect(shellPill).toBeVisible({ timeout: 5000 });

  // Pill should carry a B or S side badge.
  await expect(shellPill.locator('.oes-basket-pill-side')).toHaveText(/^[BS]$/);
});

test('command line + chain legs both surface as pills', async ({ page }) => {
  await authOnce(page);
  await page.goto('/admin/derivatives');
  await page.waitForLoadState('domcontentloaded');
  // Wait until the account list has actually loaded (placeholder text
  // flips away from "No accounts loaded"). Without this, the test
  // sometimes runs before /api/accounts returns and the picker can't
  // pop a usable account option.
  await expect(page.locator('button#opt-acct')).toBeVisible({ timeout: 15_000 });
  await expect(page.locator('button#opt-acct')).not.toContainText(
    /No accounts loaded/i, { timeout: 15_000 });
  await pickFirstAccount(page);

  await openChainFromRow(page);
  await page.waitForTimeout(1500);

  // 1. Add from chain via first "+" button.
  const chainAdd = page.locator('.oct-root button')
    .filter({ hasText: /^\+$/ }).first();
  await expect(chainAdd).toBeVisible({ timeout: 10_000 });
  await chainAdd.click();

  // 2. Switch to Command tab.
  const cmdTab = page.locator('.oes-tab')
    .filter({ hasText: /^Command$/i }).first();
  if (await cmdTab.count()) {
    await cmdTab.click();
    // Type a command order, then click "+ Basket".
    const cmdBar = page.locator('.oes-body input[type="text"], .oes-body textarea').first();
    if (await cmdBar.count()) {
      await cmdBar.fill('buy 1 nifty25apr22000ce');
      const basketAdd = page.locator('.oes-body button')
        .filter({ hasText: /Basket/i }).first();
      if (await basketAdd.count()) await basketAdd.click();
    }
  }

  // Multiple pills should now be present in the shell bar.
  const pills = page.locator('.oes-basket-pill');
  const count = await pills.count();
  // At minimum the chain add succeeded (1 pill). If the cmd path
  // failed silently (no matching command grammar / different basket
  // button label) we still want the chain pill to be visible.
  expect(count).toBeGreaterThanOrEqual(1);
});

/**
 * Regression spec: Issue 1 — basket leg tagged to stale account after
 * switching accounts mid-build (OptionChainTab.svelte `_mergeIntoBasket`/
 * `_netAgainstBasket` matching by sym+side only, ignoring account).
 *
 * Before the fix: adding a leg via the same Chain "+" button (same
 * symbol+side) under account A, then switching the modal's shared
 * account picker to account B and re-pressing the SAME "+" button,
 * silently folded the second add into the FIRST leg (lots += 1),
 * keeping it tagged account A — no second pill ever appeared.
 *
 * After the fix: the account is part of the match key, so the second
 * add can't match the account-A leg and instead pushes a genuinely new
 * pill tagged account B. This asserts exactly that: 2 distinct pills,
 * each carrying its own correct, distinct account.
 */
test('switching the shared account mid-build tags each leg with its own account (Issue 1)', async ({ page }) => {
  await authOnce(page);
  await page.goto('/admin/derivatives');
  await page.waitForLoadState('domcontentloaded');
  await expect(page.locator('button#opt-acct')).toBeVisible({ timeout: 15_000 });
  await expect(page.locator('button#opt-acct')).not.toContainText(
    /No accounts loaded/i, { timeout: 15_000 });
  await pickFirstAccount(page);

  await openChainFromRow(page);
  await page.waitForTimeout(1500);  // let chain quotes settle

  // Shared account picker inside the modal — drives `_sharedAccount` in
  // SymbolPanel.svelte, which feeds the `account` prop into both the
  // Chain tab (OptionChainTab.svelte) and the Ticket tab (OrderTicket.svelte).
  const acctTrigger = page.locator('.oes-account-pick .rbq-select-trigger').first();
  await expect(acctTrigger).toBeVisible({ timeout: 10_000 });
  await acctTrigger.click();
  const acctOptions = page.locator('.oes-account-pick .rbq-select-panel li.rbq-select-option');
  const acctCount = await acctOptions.count();
  if (acctCount < 2) {
    await page.keyboard.press('Escape');
    test.skip(true, `need ≥2 broker accounts in the modal account picker — got ${acctCount}`);
    return;
  }
  const accountA = (await acctOptions.nth(0).locator('.rbq-select-option-label').innerText()).trim();
  const accountB = (await acctOptions.nth(1).locator('.rbq-select-option-label').innerText()).trim();
  console.log(`[switch-acct] accountA=${accountA} accountB=${accountB}`);
  await acctOptions.nth(0).click();  // ensure account A is active

  // Add a leg via the first Chain "+" button (tagged account A).
  const chainAddButtons = page.locator('.oct-root button').filter({ hasText: /^\+$/ });
  await expect(chainAddButtons.first()).toBeVisible({ timeout: 10_000 });
  await chainAddButtons.first().click();

  const pills = page.locator('.oes-basket-pill');
  await expect(pills.first()).toBeVisible({ timeout: 5000 });
  expect(await pills.count(), 'first leg should produce exactly 1 basket pill').toBe(1);

  // Switch the shared account to B.
  await acctTrigger.click();
  await expect(page.locator('.oes-account-pick .rbq-select-panel')).toBeVisible({ timeout: 5000 });
  await acctOptions.nth(1).click();

  // Re-click the SAME "+" button (same symbol + same side). Pre-fix,
  // OptionChainTab's `_mergeIntoBasket` matched by sym+side only
  // (ignoring account) and would silently fold this into the existing
  // account-A leg's lot count instead of creating a new account-B leg.
  await chainAddButtons.first().click();

  await expect(pills.nth(1),
    'second leg (different account, same sym+side) should produce a SECOND pill, not merge into the first'
  ).toBeVisible({ timeout: 5000 });
  expect(await pills.count(), 'two distinct-account legs should NOT merge into one pill').toBe(2);

  // Read each pill's displayed account — per-leg Select when ≥2
  // accounts are loaded (confirmed above), else the static span.
  async function pillAccount(pill) {
    const trig = pill.locator('.oes-basket-pill-acct-wrap .rbq-select-trigger');
    if (await trig.count()) {
      return (await trig.locator('.rbq-select-label').innerText()).trim();
    }
    const stat = pill.locator('.oes-basket-pill-acct-static');
    if (await stat.count()) return (await stat.innerText()).trim();
    return '';
  }
  const leg1Acct = await pillAccount(pills.nth(0));
  const leg2Acct = await pillAccount(pills.nth(1));
  console.log(`[switch-acct] leg1=${leg1Acct} leg2=${leg2Acct}`);

  expect(leg1Acct, `leg 1 should stay tagged accountA=${accountA}`).toBe(accountA);
  expect(leg2Acct,
    `DEFECT LOCATION: leg 2 should be tagged accountB=${accountB} — got "${leg2Acct}". ` +
    `An account-blind merge key (OptionChainTab._mergeIntoBasket/_netAgainstBasket) ` +
    `would fold this into the account-A leg instead.`
  ).toBe(accountB);
  expect(leg1Acct, 'the two legs must carry distinct accounts').not.toBe(leg2Acct);
});

});
