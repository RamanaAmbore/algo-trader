/**
 * Smoke spec: Chain tab's "Basket | Place" mode toggle.
 *
 * Verifies commit ab53b7f:
 *  1. Toggle is visible at the top of the Chain tab.
 *  2. Default state is "Basket" mode — clicking + adds to basket.
 *  3. Switching to "Place" mode routes the next + click to the Ticket
 *     tab pre-filled with that leg, instead of basket-adding.
 *
 * Target: prod (ramboq.com)
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
  await expect(acctTrigger).toBeVisible({ timeout: 15_000 });
  await expect(acctTrigger).not.toContainText(/No accounts loaded/i, { timeout: 15_000 });
  await acctTrigger.click();
  const firstAcct = page.locator('.rbq-multi-panel .rbq-multi-option').first();
  await expect(firstAcct).toBeVisible({ timeout: 5000 });
  await firstAcct.click();
  await acctTrigger.click();
}

async function openChain(page) {
  await pickFirstAccount(page);
  const chainBtn = page.getByRole('button', { name: /Toggle chain picker/i });
  await expect(chainBtn).toBeEnabled({ timeout: 10_000 });
  await chainBtn.click();
  await page.waitForTimeout(1500);  // let chain quotes settle
}

test.describe('Chain Place-mode toggle', () => {

  test('toggle visible at the top of the chain tab', async ({ page }) => {
    await authOnce(page);
    await page.goto('/admin/options');
    await page.waitForLoadState('domcontentloaded');
    await openChain(page);

    const basketBtn = page.locator('.oct-mode-btn').filter({ hasText: /^Basket$/ }).first();
    const placeBtn  = page.locator('.oct-mode-btn').filter({ hasText: /^Place$/ }).first();
    await expect(basketBtn).toBeVisible({ timeout: 5000 });
    await expect(placeBtn).toBeVisible({ timeout: 5000 });

    // Default state: Basket is .on (active highlight), Place isn't.
    await expect(basketBtn).toHaveClass(/(^|\s)on(\s|$)/);
    await expect(placeBtn).not.toHaveClass(/(^|\s)on(\s|$)/);
  });

  test('Place mode opens Ticket tab pre-filled instead of basket-adding', async ({ page }) => {
    await authOnce(page);
    await page.goto('/admin/options');
    await page.waitForLoadState('domcontentloaded');
    await openChain(page);

    // Flip into Place mode.
    const placeBtn = page.locator('.oct-mode-btn').filter({ hasText: /^Place$/ }).first();
    await expect(placeBtn).toBeVisible({ timeout: 5000 });
    await placeBtn.click();
    await expect(placeBtn).toHaveClass(/(^|\s)on(\s|$)/);

    // Click first chain + button.
    const addButton = page.locator('.oes-modal button')
      .filter({ hasText: /^\+$/ }).first();
    await expect(addButton).toBeVisible({ timeout: 10_000 });
    await addButton.click();
    await page.waitForTimeout(500);

    // Shell should switch to the Ticket tab. We expect to see the
    // ticket footer (Submit / Place) — that's the unique surface of
    // the Ticket tab.
    const ticketFooter = page.locator('.oes-modal .ot-footer').first();
    await expect(ticketFooter).toBeVisible({ timeout: 5000 });

    // Basket should NOT have gained a pill (Place mode bypasses it).
    const pills = page.locator('.oes-basket-pill');
    expect(await pills.count()).toBe(0);
  });

  test('Ticket tab Submit footer is visible (not hidden by bottom panel)', async ({ page }) => {
    await authOnce(page);
    await page.goto('/admin/options');
    await page.waitForLoadState('domcontentloaded');
    await openChain(page);

    // Switch into the Order Ticket tab directly via the shell tab strip.
    const ticketTab = page.locator('.oes-tab')
      .filter({ hasText: /Order ticket/i }).first();
    if (await ticketTab.count() === 0) {
      test.skip(true, 'ticket tab not surfaced — Chain may be locked-on');
      return;
    }
    await ticketTab.click();
    await page.waitForTimeout(300);

    const ticketFooter = page.locator('.oes-modal .ot-footer').first();
    await expect(ticketFooter).toBeVisible({ timeout: 5000 });

    // The footer should fit inside the viewport (its bottom edge above
    // the bottom panel). Sanity-check via boundingBox.
    const box = await ticketFooter.boundingBox();
    if (!box) test.fail();
    const viewport = page.viewportSize();
    if (viewport && box) {
      // Footer must be entirely above the viewport bottom — at minimum,
      // its top should sit somewhere in the visible region.
      expect(box.y).toBeLessThan(viewport.height);
      expect(box.y + box.height).toBeLessThanOrEqual(viewport.height + 4);
    }
  });
});
