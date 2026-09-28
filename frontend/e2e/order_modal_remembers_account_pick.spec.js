/**
 * order_modal_remembers_account_pick.spec.js
 *
 * Operator: "Order chain and ticket are not considering the account
 * number while placing orders. All orders are routed to only one kite
 * account."
 *
 * Root cause: PageHeaderActions.svelte's _effectiveAccount (used to
 * pre-select the account every time the global "+ Order" modal opens)
 * was `$derived(resolveAccount(...))`. resolveAccount() (accounts.js)
 * reads localStorage directly — a plain, non-reactive read. Inside a
 * $derived, Svelte only re-runs when a TRACKED reactive dependency
 * changes; that internal localStorage read isn't one, so
 * _effectiveAccount computed once (the first time the accounts list
 * loaded) and never updated again for the rest of the session — every
 * subsequent modal open silently reseeded from that frozen value
 * regardless of which account the operator had since picked.
 *
 * Fix: read the reactive recentAccountStore/defaultAccountStore
 * directly (the same stores setRecentAccount()/loadAccounts() write
 * to) instead of the frozen resolveAccount() call.
 *
 * This spec opens the header order modal, records the pre-selected
 * account, switches to a DIFFERENT account via the picker, closes the
 * modal, reopens it, and asserts the picker now pre-selects the
 * NEWLY-chosen account — not the original one.
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=http://localhost:5173 \
 *     npx playwright test e2e/order_modal_remembers_account_pick.spec.js \
 *     --project=chromium-desktop
 */
import { test, expect } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

let _cachedToken = null;
async function login(page) {
  if (!_cachedToken) {
    for (const u of ['rambo', 'ambore', 'admin']) {
      const r = await page.request.post(`${BASE}/api/auth/login`, {
        data: { username: u, password: _AUTH_PASS },
        timeout: 15_000,
      }).catch(() => null);
      if (r && r.ok()) { _cachedToken = (await r.json()).access_token; break; }
    }
    if (!_cachedToken) throw new Error('login failed');
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, _cachedToken);
}

test('order modal remembers the operator\'s account pick across re-opens', async ({ page }) => {
  test.setTimeout(60_000);
  await login(page);
  await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2000);

  const openModal = async () => {
    await page.locator('button.pha-order').first().click({ force: true });
    await page.waitForTimeout(1000);
  };
  const closeModal = async () => {
    const closeBtn = page.locator('.oes-close').first();
    if (await closeBtn.count() > 0) await closeBtn.click();
    await page.waitForTimeout(500);
  };

  await openModal();

  const acctSelect = page.locator('.oes-account-pick .rbq-select-trigger').first();
  if (await acctSelect.count() === 0) {
    test.skip(true, 'no account picker rendered (single-account operator?) — nothing to switch, skip');
    return;
  }

  const initialLabel = (await page.locator('.oes-account-pick .rbq-select-label').first().textContent())?.trim();

  await acctSelect.click();
  await page.waitForTimeout(300);
  const options = page.locator('.oes-account-pick [role="option"], .oes-account-pick .rbq-select-option');
  const optCount = await options.count();
  let target = null;
  for (let i = 0; i < optCount; i++) {
    const txt = (await options.nth(i).textContent())?.trim();
    if (txt && txt !== initialLabel) { target = { idx: i, label: txt }; break; }
  }
  if (!target) {
    test.skip(true, 'only one account available — nothing to switch to, skip');
    return;
  }
  await options.nth(target.idx).click();
  await page.waitForTimeout(500);

  const afterPickLabel = (await page.locator('.oes-account-pick .rbq-select-label').first().textContent())?.trim();
  expect(afterPickLabel).toBe(target.label);

  await closeModal();
  await page.waitForTimeout(500);
  await openModal();

  const reopenedLabel = (await page.locator('.oes-account-pick .rbq-select-label').first().textContent())?.trim();
  expect(
    reopenedLabel,
    `expected the reopened modal to pre-select the just-picked account ` +
    `(${target.label}), got "${reopenedLabel}" — the picker reverted to ` +
    `the original/frozen default instead of remembering the operator's pick`
  ).toBe(target.label);
});
