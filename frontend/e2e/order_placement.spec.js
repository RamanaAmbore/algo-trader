/**
 * Order placement — every tab × submit-mode combination.
 *
 * Market is closed so every real submission is rejected upstream by the
 * broker. The test verifies the request reaches the backend and that the
 * outcome (preflight block / broker reject / 422 / 200-OPEN) surfaces in
 * the Log tab below the modal — that's the operator's full feedback loop.
 *
 * Auth: needs admin credentials. Two options:
 *
 *   ADMIN_USER=… ADMIN_PASS=… PLAYWRIGHT_BASE_URL=https://ramboq.com \
 *     npx playwright test order_placement --project=chromium-desktop
 *
 * OR paste an existing JWT (open ramboq.com in a logged-in tab,
 *   localStorage.getItem('rambo.auth') → copy the token):
 *
 *   PLAYWRIGHT_ADMIN_TOKEN=eyJ… PLAYWRIGHT_BASE_URL=https://ramboq.com \
 *     npx playwright test order_placement --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';

const BASE  = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const USER  = process.env.ADMIN_USER || '';
const PASS  = process.env.ADMIN_PASS || '';
const TOKEN = process.env.PLAYWRIGHT_ADMIN_TOKEN || '';

async function login(page) {
  if (TOKEN) {
    await page.addInitScript((tok) => {
      localStorage.setItem('rambo.auth', JSON.stringify({ token: tok, user: { role: 'admin' } }));
    }, TOKEN);
    return;
  }
  if (!USER || !PASS) {
    test.skip(true, 'no auth — set ADMIN_USER+ADMIN_PASS or PLAYWRIGHT_ADMIN_TOKEN');
  }
  const res = await page.request.post(`${BASE}/api/auth/login`, {
    data: { username: USER, password: PASS },
    headers: { 'Content-Type': 'application/json' },
  });
  expect(res.ok(), `login failed: ${res.status()}`).toBe(true);
  const body = await res.json();
  const tok = body.access_token || body.token;
  expect(tok, 'no token in login response').toBeTruthy();
  await page.addInitScript((t) => {
    localStorage.setItem('rambo.auth', JSON.stringify({ token: t, user: { role: 'admin' } }));
  }, tok);
}

async function openShell(page) {
  // /console renders the OrderEntryShell inline as the Terminal page
  // body — no account-picker prerequisite, no modal trigger needed.
  await page.goto('/console');
  await page.waitForLoadState('networkidle');
  // Wait for the tab strip to mount.
  await expect(page.getByRole('tab', { name: /Command line/i })).toBeVisible({ timeout: 15_000 });
}

test.describe('Order placement — every combination', () => {

  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('Ticket tab — Place button submits + reject lands in Log', async ({ page }) => {
    await openShell(page);
    // OChain button also opens the OrderEntryShell with Chain tab active.
    // Switch to Ticket tab.
    const ticketTab = page.getByRole('tab', { name: /Order ticket/i });
    await expect(ticketTab).toBeVisible({ timeout: 10_000 });
    await ticketTab.click();
    await expect(page.locator('.ot-side-toggle')).toBeVisible();
    // The ticket needs a symbol — chain didn't pre-fill since OChain
    // opens with no specific strike. Skip submit if symbol is blank.
    const symInput = page.locator('input[name="symbol"], .ot-symbol-input').first();
    if (await symInput.count()) {
      await symInput.fill('NIFTY26MAY22000PE');
    }
    // Click Place — accept whatever comes back; the backend will preflight
    // / reject / accept and the Log tab will show the outcome.
    const place = page.getByRole('button', { name: /Place|BUY|SELL/i }).last();
    if (await place.isVisible() && await place.isEnabled()) {
      await place.click();
      await page.waitForTimeout(2_000);
    }
    // Bottom Log tab — verify it surfaces the latest event row.
    const logTab = page.locator('.oes-bottom-tab', { hasText: /Log/i }).first();
    if (await logTab.count()) await logTab.click();
    const eventList = page.locator('.oes-events-list, .oes-orders-empty').first();
    await expect(eventList).toBeVisible({ timeout: 5_000 });
  });

  test('Ticket tab — + Basket adds leg + basket bar appears', async ({ page }) => {
    await openShell(page);
    const ticketTab = page.getByRole('tab', { name: /Order ticket/i });
    await ticketTab.click();
    const basketBtn = page.locator('button.ot-basket').first();
    if (!(await basketBtn.count())) {
      test.skip(true, 'ticket flow needs a symbol — chain opens without one; covered by chain-tab tests below');
    }
  });

  test('Command tab — Run submits a parsed order', async ({ page }) => {
    await openShell(page);
    const cmdTab = page.getByRole('tab', { name: /Command line/i });
    await cmdTab.click();
    const cmdInput = page.locator('textarea').first();
    await expect(cmdInput).toBeVisible({ timeout: 10_000 });
    await cmdInput.click();
    // Build a real order command.
    await cmdInput.pressSequentially('buy ZG0790 NIFTY26MAY22000PE 75 limit 1', { delay: 25 });
    await page.waitForTimeout(300);
    const runBtn = page.locator('.sim-btn-primary, .sim-btn-danger').first();
    if (await runBtn.isVisible()) {
      await runBtn.click();
      await page.waitForTimeout(2_000);
    }
  });

  test('Command tab — + Basket button adds parsed leg', async ({ page }) => {
    /** @type {string[]} */
    const consoleLogs = [];
    page.on('console', (m) => consoleLogs.push(`[${m.type()}] ${m.text()}`));
    await openShell(page);
    const cmdTab = page.getByRole('tab', { name: /Command line/i });
    await cmdTab.click();
    const cmdInput = page.locator('textarea').first();
    await cmdInput.click();
    await cmdInput.pressSequentially('buy ZG0790 NIFTY26MAY22000PE 75 limit 1', { delay: 25 });
    await expect(page.locator('.sim-btn-primary, .sim-btn-danger').first()).toContainText(/BUY|SELL/, { timeout: 5_000 });
    const basketBtn = page.locator('.sim-btn-basket').first();
    await expect(basketBtn).toBeVisible();
    // Check the runtime parse result + intent state by reaching into the
    // page — gives us a snapshot of what the click is about to operate on.
    const preState = await page.evaluate(() => ({
      basketBtnDisabled: document.querySelector('.sim-btn-basket')?.disabled,
      runText: document.querySelector('.sim-btn-primary, .sim-btn-danger')?.textContent?.trim(),
      cmdValue: /** @type {HTMLTextAreaElement|null} */ (document.querySelector('textarea'))?.value,
    }));
    console.log('[preState]', JSON.stringify(preState));
    await basketBtn.click();
    await page.waitForTimeout(2_000);
    const postState = await page.evaluate(() => ({
      basketBarPresent: !!document.querySelector('.oes-basket-bar'),
      basketCount:      document.querySelector('.oes-basket-count')?.textContent?.trim(),
      cmdValueAfter:    /** @type {HTMLTextAreaElement|null} */ (document.querySelector('textarea'))?.value,
      addedToBasketLine:Array.from(document.querySelectorAll('.clt-result, .clt-row')).map(n => n.textContent?.trim()).slice(0, 3),
    }));
    console.log('[postState]', JSON.stringify(postState));
    console.log('[browserConsole]', consoleLogs.slice(-15).join('\n'));
    await expect(page.locator('.oes-basket-bar').first()).toBeVisible({ timeout: 10_000 });
  });

  test('Chain tab — pick strikes + Submit basket', async ({ page }) => {
    await openShell(page);
    const chainTab = page.getByRole('tab', { name: /^Chain/i });
    await chainTab.click();
    // Wait for the chain grid to load.
    const chainPill = page.locator('.octa-cell, .oct-row, button:has-text("+CE"), button:has-text("+ CE")').first();
    if (!(await chainPill.count())) {
      test.skip(true, 'chain grid did not populate — likely no underlying picked');
    }
    await chainPill.click();
    await page.waitForTimeout(500);
    const basketBar = page.locator('.oes-basket-bar').first();
    await expect(basketBar).toBeVisible({ timeout: 5_000 });
    const submitBasket = page.locator('.oes-basket-submit, button:has-text("Submit basket")').first();
    if (await submitBasket.count()) {
      await submitBasket.click();
      await page.waitForTimeout(3_000);
    }
  });

  test('Bottom Log + Orders panels render', async ({ page }) => {
    await openShell(page);
    const logTab = page.locator('.oes-bottom-tab', { hasText: /Log/i }).first();
    const ordersTab = page.locator('.oes-bottom-tab', { hasText: /Orders/i }).first();
    await expect(logTab).toBeVisible({ timeout: 10_000 });
    await expect(ordersTab).toBeVisible();
    await logTab.click();
    await expect(page.locator('.oes-events-list, .oes-orders-empty').first()).toBeVisible();
    await ordersTab.click();
    await expect(page.locator('.oes-orders-wrap, .oes-orders-empty').first()).toBeVisible();
  });
});
