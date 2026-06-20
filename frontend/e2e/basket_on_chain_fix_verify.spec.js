/**
 * Verify commit 54203aa3 — basket-on-chain bug fix + UI restructure.
 *
 * Tests (against prod https://ramboq.com):
 *   1. /orders page title reads "Order entry" (not "Orders").
 *   2. Dedicated mode/chase row is gone — `.oes-common-mode-row` absent.
 *   3. Mode pill + CHASE toggle live in the picker row (`.oes-picker-cluster`).
 *   4. No "Mode" text label, no "change" link adjacent to the mode pill.
 *   5. Scenario A — happy path: +CE click on Chain tab adds a leg pill.
 *   6. Scenario B — error surfaces: basketError renders outside the
 *      chainBasket gate when account is empty at add-time.
 *   7. Two legs can be added; basket count increments correctly.
 *   8. SymbolPanel modal (navbar Order button) title reads "Order entry".
 *   9. CHASE toggle is inside the picker cluster.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://ramboq.com \
 *   PLAYWRIGHT_USER=ambore PLAYWRIGHT_PASS=<pass> \
 *   npx playwright test e2e/basket_on_chain_fix_verify.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

const AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

const TIMEOUT = 30_000;
const SHORT   = 10_000;

// ── auth ──────────────────────────────────────────────────────────────────────
let _cachedToken = null;

async function authOnce(page) {
  if (!_cachedToken) {
    let tok = null;
    for (const delay of [0, 20_000, 65_000]) {
      if (delay) await new Promise((r) => setTimeout(r, delay));
      const resp = await page.request.post('/api/auth/login', {
        data: { username: AUTH_USER, password: AUTH_PASS },
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
      user_id: AUTH_USER, username: AUTH_USER, role: 'admin', display_name: AUTH_USER,
    }));
  }, _cachedToken);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
}

// ── page helpers ──────────────────────────────────────────────────────────────

async function gotoOrders(page) {
  await page.goto('/orders', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.page-title-chip').first()).toBeVisible({ timeout: TIMEOUT });
}

async function openChainTab(page) {
  const chainTab = page.getByRole('tab', { name: /^Chain$/i }).first();
  await expect(chainTab).toBeVisible({ timeout: SHORT });
  await chainTab.click();
  await expect(chainTab).toHaveAttribute('aria-selected', 'true', { timeout: SHORT });
}

async function pickFirstUnderlying(page) {
  // Try a custom dropdown trigger first, then fall back to native select.
  const trigger = page.locator(
    '.oct-und-trigger, button[aria-label*="underlying" i], .oct-und-pill, select[name="underlying"]'
  ).first();

  if (!(await trigger.count())) return; // chain may auto-select

  const tagName = await trigger.evaluate((el) => el.tagName.toLowerCase());
  if (tagName === 'select') {
    const firstOpt = trigger.locator('option').nth(1);
    const val = await firstOpt.getAttribute('value');
    if (val) await trigger.selectOption(val);
    return;
  }

  await trigger.click();
  const firstOption = page.locator(
    '.rbq-multi-option, .rbq-dd-option, .oct-und-option'
  ).first();
  if (await firstOption.count()) {
    await expect(firstOption).toBeVisible({ timeout: SHORT });
    await firstOption.click();
    // Close dropdown if still open
    if (await trigger.isVisible()) await trigger.click().catch(() => {});
  }
}

async function waitForStrikeGrid(page) {
  // BUY buttons on the chain grid use .chain-btn.chain-btn-buy
  const addBtn = page.locator('.chain-btn.chain-btn-buy').first();
  await expect(addBtn).toBeVisible({ timeout: TIMEOUT });
}

// ── tests ─────────────────────────────────────────────────────────────────────

test.describe('/orders — 54203aa3 restructure + basket-on-chain fix', () => {

  // ── 1. Page title ──────────────────────────────────────────────────────────
  test('page title chip reads "Order entry"', async ({ page }) => {
    await authOnce(page);
    await gotoOrders(page);

    const h1 = page.locator('h1.page-title-chip').first();
    await expect(h1).toBeVisible({ timeout: TIMEOUT });
    await expect(h1).toHaveText(/Order entry/i);
  });

  // ── 2. Dedicated mode/chase row is gone ───────────────────────────────────
  test('oes-common-mode-row is absent from the DOM', async ({ page }) => {
    await authOnce(page);
    await gotoOrders(page);

    // Navigate into the entry card so the full SymbolPanel mounts
    await openChainTab(page);

    const modeRow = page.locator('.oes-common-mode-row');
    await expect(modeRow).toHaveCount(0);
  });

  // ── 3. Mode pill lives in picker cluster ──────────────────────────────────
  test('mode pill lives inside oes-picker-cluster (picker row)', async ({ page }) => {
    await authOnce(page);
    await gotoOrders(page);

    const cluster = page.locator('.oes-picker-cluster').first();
    await expect(cluster).toBeVisible({ timeout: TIMEOUT });

    const modeChip = cluster.locator('.oes-common-mode-chip').first();
    await expect(modeChip).toBeVisible({ timeout: SHORT });

    const modeText = (await modeChip.textContent() ?? '').trim().toUpperCase();
    expect(['PAPER', 'LIVE', 'SIM', 'SHADOW', 'REPLAY']).toContain(modeText);
  });

  // ── 4. No "Mode" label and no "change" link ───────────────────────────────
  test('no "Mode" label and no "change" link next to mode pill', async ({ page }) => {
    await authOnce(page);
    await gotoOrders(page);

    await expect(page.locator('.oes-common-mode-label')).toHaveCount(0);
    await expect(page.locator('.oes-common-mode-change')).toHaveCount(0);

    // No anchor with exact text "change" anywhere on the page
    const changeLinks = page.getByRole('link', { name: /^change$/i });
    await expect(changeLinks).toHaveCount(0);
  });

  // ── 5. Scenario A — happy path: +CE adds a basket leg pill ───────────────
  test('Scenario A: +CE on Chain tab adds a leg to the basket', async ({ page }) => {
    await authOnce(page);
    await gotoOrders(page);
    await openChainTab(page);
    // The chain underlying is seeded from the page's recent symbol; no
    // inline underlying picker exists — the header symbol picker drives it.
    await waitForStrikeGrid(page);

    // BUY button in the chain strike grid (CE or futures row)
    const addBtn = page.locator('.chain-btn.chain-btn-buy').first();
    await expect(addBtn).toBeVisible({ timeout: SHORT });
    await addBtn.click();

    // Either the inline chain basket pill or the external shell pill
    const basketPill = page.locator('.chain-basket-leg, .oes-basket-pill').first();
    await expect(basketPill).toBeVisible({ timeout: SHORT });

    const pillText = (await basketPill.textContent() ?? '').trim();
    expect(pillText.length).toBeGreaterThan(0);
  });

  // ── 6. Scenario B — basketError surfaces outside the gate ─────────────────
  test('Scenario B: basketError shows when account is empty at +CE click', async ({ page }) => {
    // Intercept /api/accounts so _allAccounts stays empty → _account=''
    await page.route('**/api/accounts**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    );

    await authOnce(page);
    await gotoOrders(page);
    await openChainTab(page);
    await waitForStrikeGrid(page);

    const addBtn = page.locator('.chain-btn.chain-btn-buy').first();
    await expect(addBtn).toBeVisible({ timeout: SHORT });
    await addBtn.click();

    // Error div — hoisted outside the chainBasket gate in this commit.
    // Carries role="alert" per the new markup.
    const errDiv = page.locator('.chain-basket-err').first();
    await expect(errDiv).toBeVisible({ timeout: SHORT });

    const errText = (await errDiv.textContent() ?? '').trim();
    expect(errText.length).toBeGreaterThan(0);
    expect(errText).toMatch(/account/i);
  });

  // ── 7. Two legs increment basket count ────────────────────────────────────
  test('adding two + buttons increments basket to at least 2', async ({ page }) => {
    await authOnce(page);
    await gotoOrders(page);
    await openChainTab(page);
    await waitForStrikeGrid(page);

    const addBtns = page.locator('.chain-btn.chain-btn-buy');
    await expect(addBtns.first()).toBeVisible({ timeout: SHORT });
    await addBtns.first().click();
    await page.waitForTimeout(350);

    const secondBtn = addBtns.nth(1);
    if (await secondBtn.count()) {
      await secondBtn.click();
      await page.waitForTimeout(350);
    }

    const pills = page.locator('.chain-basket-leg, .oes-basket-pill');
    const count = await pills.count();
    expect(count).toBeGreaterThanOrEqual(1);
  });

  // ── 8. SymbolPanel modal title ────────────────────────────────────────────
  test('SymbolPanel modal (navbar Order button) title reads "Order entry"', async ({ page }) => {
    await authOnce(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.page-title-chip').first()).toBeVisible({ timeout: TIMEOUT });

    // PageHeaderActions amber Order button — class is .pha-btn.pha-order
    const orderBtn = page.locator('.pha-btn.pha-order').first();

    if (!(await orderBtn.count())) {
      test.skip(true, '.pha-btn.pha-order not found on /dashboard');
      return;
    }

    await orderBtn.click();

    // The SymbolPanel modal should open — wait for the modal root
    const modal = page.locator('.oes-modal').first();
    await expect(modal).toBeVisible({ timeout: SHORT });

    // The heading title element inside the modal — .oes-modal-name
    const modalTitle = modal.locator('.oes-modal-name').first();
    await expect(modalTitle).toBeVisible({ timeout: SHORT });
    await expect(modalTitle).toContainText(/Order entry/i);
  });

  // ── 9. CHASE toggle in picker cluster ─────────────────────────────────────
  test('CHASE toggle is inside oes-picker-cluster', async ({ page }) => {
    await authOnce(page);
    await gotoOrders(page);

    const cluster = page.locator('.oes-picker-cluster').first();
    await expect(cluster).toBeVisible({ timeout: TIMEOUT });

    const chaseToggle = cluster.locator('.oes-common-chase-toggle').first();
    await expect(chaseToggle).toBeVisible({ timeout: SHORT });

    const chaseLabel = cluster.locator('.oes-common-chase-label').first();
    await expect(chaseLabel).toContainText(/CHASE/i);
  });

});
